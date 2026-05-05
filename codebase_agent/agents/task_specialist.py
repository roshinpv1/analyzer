"""
Task Specialist Agent for AutoGen Codebase Understanding Agent.

This module implements the Task Specialist agent responsible for reviewing
analysis completeness and providing abstract feedback to guide further analysis.
"""

import json
import logging
import re

from autogen_agentchat.agents import AssistantAgent

from ..utils.autogen_utils import (
    extract_text_from_autogen_response,
    run_assistant_single_turn,
)


class TaskSpecialist:
    """
    Task Specialist agent that evaluates analysis reports from the perspective of
    an engineer who needs to implement the requested task.

    The Task Specialist acts as the engineer who will receive the analysis report
    and execute the task, evaluating whether the report provides sufficient
    actionable information to begin implementation immediately without additional
    codebase investigation.
    """

    def __init__(
        self,
        config: dict,
        playbook_instructions: str | None = None,
        structured_logger=None,
        max_reviews: int = 3,
        first_review_min_confidence: float = 0.85,
    ):
        """
        Initialize the Task Specialist agent.

        Args:
            config: Configuration dict containing model settings
            playbook_instructions: Optional strategic instructions from a playbook
            structured_logger: Optional logger for session-level audit trails
            max_reviews: Review rounds before forced acceptance (must match manager cap)
            first_review_min_confidence: Min confidence on round 1 to accept without rework
        """
        self.config = config
        self.playbook_instructions = playbook_instructions
        self.structured_logger = structured_logger
        self.logger = logging.getLogger(__name__)

        # Review tracking (aligned with AgentManager.MAX_SPECIALIST_REVIEWS / env)
        self.review_count = 0
        self.max_reviews = max(1, int(max_reviews))
        self.first_review_min_confidence = float(
            max(0.5, min(0.99, first_review_min_confidence))
        )

        # Initialize AutoGen agent
        self._agent = self._create_autogen_agent()

    def _create_autogen_agent(self) -> AssistantAgent:
        """Create and configure the AutoGen AssistantAgent."""
        system_message = self._get_system_message()

        agent = AssistantAgent(
            name="task_specialist",
            system_message=system_message,
            model_client=self.config,
        )

        return agent

    def _get_system_message(self) -> str:
        """Get the system message for the Task Specialist agent."""
        base_message = """You are a Task Specialist - a RUTHLESS TECHNICAL AUDITOR who ensures codebase analysis is deep, accurate, and actionable. Your job is to reject superficial reports and demand engineering-grade evidence.

### 🧠 AUDIT PHILOSOPHY
- **No Fluff**: Reject meaningless buzzwords like "sophisticated", "excellent", or "comprehensive" unless backed by SPECIFIC code examples and implementation details.
- **Evidence-First**: If a report claims a feature exists, it must specify the EXACT files and logic patterns that implement it.
- **Actionability**: Ask yourself: "Can an engineer start coding based on this report ALONE?" If the answer is no, REJECT.

### 🎯 EVALUATION CRITERIA
1. **Technical Precision**: Does the report identify exact method signatures, class structures, and integration patterns?
2. **Data Flow Awareness**: Does it explain how information moves through the system, not just what files exist?
3. **Logic Validation**: Has the analyzer actually read the logic, or is it just listing imports and filenames?
4. **Boundary conditions**: Are error handling, configuration defaults, and edge cases addressed?

### 🛑 REJECTION PROTOCOL
- Reject if the report is just a summary of file paths.
- Reject if the analysis relies on "hallucinated" assumptions without `read_file` evidence.
- Reject if the report fails to address the specific objectives of the current playbook.

### 📋 RESPONSE FORMAT
You MUST respond with a single JSON object:
```json
{
    "is_complete": true/false,
    "feedback": "Specific, actionable technical guidance for the analyzer...",
    "confidence": 0.0 to 1.0
}
```
"""

        if self.playbook_instructions:
            base_message += f"\n\n🚀 STRATEGIC PLAYBOOK GUIDANCE:\n{self.playbook_instructions}\n"
            base_message += "\nIMPORTANT: The above playbook provides the SPECIFIC AUDIT CRITERIA for this task. Enforce these requirements strictly while maintaining your high technical standards.\n"

        return base_message

    def review_analysis(
        self, analysis_report: str, task_description: str, current_review_count: int
    ) -> tuple[bool, str, float]:
        """
        Review analysis report from the perspective of an engineer who needs to implement the task.

        Evaluates whether the report provides sufficient actionable information for
        immediate implementation without requiring additional codebase investigation.

        Args:
            analysis_report: The analysis report to review
            task_description: Original task description
            current_review_count: Current review iteration (1-based)

        Returns:
            Tuple of (is_complete, feedback_message, confidence_score)
        """
        self.review_count = current_review_count

        self.logger.info(
            f"Starting Task Specialist review {self.review_count}/{self.max_reviews}"
        )

        # Force accept if maximum reviews reached with stricter confidence penalty
        if self.review_count >= self.max_reviews:
            self.logger.warning(
                "Maximum reviews reached - returning explicit rejection"
            )
            return (
                False,
                "Maximum review limit reached without sufficient quality evidence.",
                0.5,
            )

        # Primary path: Ask the LLM to perform the review with a structured prompt
        try:
            review_prompt = self._build_review_prompt(
                task_description, analysis_report, self.review_count
            )

            llm_response = run_assistant_single_turn(self._agent, review_prompt)
            is_complete, feedback, confidence = self._parse_llm_review_response(
                llm_response
            )

            # If parsing succeeded, honor LLM decision but apply minimum confidence threshold
            if feedback:
                # Apply stricter confidence threshold for acceptance
                min_confidence_for_acceptance = 0.80

                if self.review_count == 1:
                    min_confidence_for_acceptance = self.first_review_min_confidence
                    self.logger.info(
                        "First review - applying confidence threshold (%.2f)",
                        min_confidence_for_acceptance,
                    )

                if is_complete and confidence < min_confidence_for_acceptance:
                    self.logger.warning(
                        f"LLM accepted but confidence {confidence:.2f} below threshold {min_confidence_for_acceptance}"
                    )
                    is_complete = False
                    feedback = f"Analysis needs improvement. {feedback} (Confidence {confidence:.2f} below required {min_confidence_for_acceptance})"

                self.logger.info(
                    f"LLM review completed. Decision: {'ACCEPT' if is_complete else 'REJECT'} "
                    f"(confidence={confidence:.2f})"
                )
                return is_complete, feedback, confidence
        except Exception as e:
            # Fall back to heuristic assessment on any failure
            self.logger.warning(f"LLM-driven review error: {e}")

        # If we couldn't parse or call the LLM appropriately, return a neutral rejection
        # without applying any hardcoded judgement logic.
        return (
            False,
            "Analysis review could not be completed due to unparsable LLM response. Please retry the review step.",
            0.0,
        )

    def _build_review_prompt(
        self, task_description: str, analysis_report: str, review_number: int
    ) -> str:
        """Build a structured prompt instructing the LLM to review and respond in JSON.

        The LLM must decide completeness per the review criteria and return a JSON object:
        {"is_complete": bool, "feedback": str, "confidence": float}
        """
        # Extract only the FINAL ANALYSIS section for evaluation
        final_analysis = self._extract_final_analysis(analysis_report)

        return f"""
You are a CODE REVIEW SPECIALIST evaluating analysis reports. Think like a tech lead who has to implement this task.

TASK: {task_description}

ANALYSIS TO EVALUATE:
{final_analysis}

CORE QUESTION: "Can I start implementing this task immediately, or do I need to investigate the codebase further?"

QUALITY REQUIREMENTS:
1. SYSTEM UNDERSTANDING: Explains HOW components work together, not just what they are
2. ENTRY POINTS: Shows WHERE to start investigating or modifying code
3. DATA FLOW: Describes how information flows through the system
4. TASK RELEVANCE: Connects analysis directly to the requested task

REJECT IMMEDIATELY IF:
- Lists components without explaining interactions
- Shows code examples instead of architectural understanding
- Provides generic advice not specific to this codebase
- Missing explanation of how the system actually operates
- No clear guidance on where to focus for this specific task

CONFIDENCE SCORING:
- 0.9+: Ready to implement - clear system understanding and task guidance
- 0.8-0.89: Good foundation but missing some implementation details
- 0.7-0.79: Basic overview but needs significant additional investigation
- Below 0.7: Inadequate - requires major additional analysis

FEEDBACK RULES:
- **LOOP DETECTION**: If you see the agent repeating the same tool calls (files or graph) or failing to progress for 2+ cycles, EXPLICITLY tell it to stop searching and synthesize a best-effort answer with the data it has.
- **GRAPH SPAM**: If the analyzer is calling graph tools with the same questions multiple times, REJECT and instruct it to use `list_directory` or `read_file` instead.
- For REJECTIONS: Provide specific file system operations (search_content, fuzzy_search, read_file) or graph queries to fill gaps. Do NOT suggest bash/shell commands like grep or find.
- For ACCEPTANCE: Briefly confirm what makes it ready for implementation

RESPONSE FORMAT:
JSON only: {{"is_complete": boolean, "feedback": "specific actionable guidance", "confidence": float}}

REJECTION EXAMPLES:
{{"is_complete": false, "feedback": "Missing data flow. Use search_content for 'def process\\|def handle' or fuzzy_search for 'request handler' to find entry points, then trace how requests flow through the system", "confidence": 0.35}}

{{"is_complete": false, "feedback": "Component interactions unclear. Use graph_queries (god_nodes) or fuzzy_search to examine dependency injection patterns", "confidence": 0.40}}

ACCEPTANCE EXAMPLE:
{{"is_complete": true, "feedback": "Clear system operation explanation with task-specific entry points identified", "confidence": 0.87}}
"""

    def _extract_final_analysis(self, analysis_report: str) -> str:
        """Extract only the FINAL ANALYSIS section from the complete report."""
        import re

        # Look for FINAL ANALYSIS section
        final_analysis_pattern = r"FINAL ANALYSIS:\s*(.*?)(?=\n\s*EXECUTION SUMMARY:|$)"
        match = re.search(
            final_analysis_pattern, analysis_report, re.DOTALL | re.IGNORECASE
        )

        if match:
            final_analysis = match.group(1).strip()
            if final_analysis:
                return final_analysis

        # Fallback: if we can't find the section, return a note about missing analysis
        return "No FINAL ANALYSIS section found in the report."

    def _parse_llm_review_response(self, raw_response) -> tuple[bool, str, float]:
        """Parse the LLM response and extract the JSON decision.

        Supports plain JSON or fenced code blocks. Falls back to empty feedback on failure.
        """
        # Handle TaskResult object from agent.run()
        response_text = extract_text_from_autogen_response(raw_response)

        if not isinstance(response_text, str):
            response_text = str(response_text)

        # Try to extract JSON object from the response
        json_text = None

        # 1) Exact JSON on first line
        first_line = (
            response_text.strip().splitlines()[0] if response_text.strip() else ""
        )
        if first_line.startswith("{") and first_line.endswith("}"):
            json_text = first_line
        else:
            # 2) Look for fenced JSON ```json ... ``` or any {...}
            fenced = re.search(
                r"```(?:json)?\s*(\{[\s\S]*?\})\s*```", response_text, re.IGNORECASE
            )
            if fenced:
                json_text = fenced.group(1)
            else:
                obj = re.search(r"(\{[\s\S]*\})", response_text)
                if obj:
                    json_text = obj.group(1)

        if not json_text:
            return False, "", 0.0

        try:
            data = json.loads(json_text)
            is_complete = bool(data.get("is_complete", False))
            feedback = str(data.get("feedback", "")).strip()
            confidence_raw = data.get("confidence", 0.0)
            try:
                confidence = float(confidence_raw)
            except (TypeError, ValueError):
                confidence = 0.0

            # Clamp confidence to [0,1]
            confidence = max(0.0, min(1.0, confidence))

            return is_complete, feedback, confidence
        except Exception:
            return False, "", 0.0

    @property
    def agent(self) -> AssistantAgent:
        """Get the underlying AutoGen agent."""
        return self._agent
