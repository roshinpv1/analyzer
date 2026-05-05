"""
Code Analyzer Agent for AutoGen Codebase Understanding Agent.

This module implements the Code Analyzer agent responsible for technical analysis
of codebases using multi-round self-iteration and file system operations.
"""

import logging
import re

from autogen_agentchat.agents import AssistantAgent

from ..utils.autogen_utils import (
    extract_text_from_autogen_response,
    run_assistant_single_turn,
)

# Max analyzer LLM iterations per specialist round (single source of truth for budgeting / UX)
DEPTH_PROFILE_MAX_ITERATIONS = {
    "quick": 6,
    "standard": 12,
    "deep": 20,
    "forensic": 28,
    # doc: broad-but-shallow exploration, then hand off to synthesis for the wiki
    "doc": 8,
}


def iteration_cap_for_depth_profile(profile: str) -> int:
    """Upper bound on analyze_codebase LLM iterations for a depth profile."""
    p = profile if profile in DEPTH_PROFILE_MAX_ITERATIONS else "standard"
    return DEPTH_PROFILE_MAX_ITERATIONS[p]


class CodeAnalyzer:
    """
    Technical expert agent responsible for codebase analysis using file system operations
    and iterative exploration.

    The Code Analyzer performs multi-round self-iteration to progressively analyze
    codebases, building knowledge through targeted file system operations and
    self-assessment of analysis completeness.
    """

    def __init__(
        self,
        config: dict,
        file_system_tool,
        graphify_tool=None,
        playbook_instructions=None,
        playbook_metadata: dict | None = None,
        structured_logger=None,
        memory_tool=None,
        session_id=None,
        analysis_depth_profile: str = "standard",
    ):
        """
        Initialize the Code Analyzer agent.

        Args:
            config: Configuration dict containing model settings
            file_system_tool: Shell execution tool for codebase exploration
            graphify_tool: Tool for interacting with the knowledge graph
            playbook_instructions: Optional strategic instructions from a playbook
            structured_logger: Optional logger for session-level audit trails
            memory_tool: Optional mem0-based memory layer
            session_id: Unique session identifier for memory isolation
        """
        self.config = config
        self.file_system_tool = file_system_tool
        self.graphify_tool = graphify_tool
        self.playbook_instructions = playbook_instructions
        self.playbook_metadata: dict = playbook_metadata or {}
        self.structured_logger = structured_logger
        self.memory_tool = memory_tool
        self.session_id = session_id
        self.logger = logging.getLogger(__name__)  # must be set before any self.logger calls below
        self.analysis_depth_profile = (
            analysis_depth_profile
            if analysis_depth_profile in {"quick", "standard", "deep", "forensic", "doc"}
            else "standard"
        )
        # Playbook frontmatter can override the inferred depth profile.
        pb_depth = str(self.playbook_metadata.get("depth_profile", "") or "").strip()
        if pb_depth in {"quick", "standard", "deep", "forensic", "doc"}:
            self.logger.info(
                "Playbook overrides depth_profile: %s → %s",
                self.analysis_depth_profile, pb_depth,
            )
            self.analysis_depth_profile = pb_depth
        self.depth_budgets = self._get_depth_budgets(self.analysis_depth_profile)
        # Whether to skip mid-run milestone summary LLM calls (saves 1-2 extra calls).
        self.disable_milestone_summaries: bool = bool(
            self.playbook_metadata.get("disable_milestone_summaries", False)
        )
        # Allow playbook frontmatter to override the default citation threshold.
        # e.g. documentation playbooks set citation_coverage_threshold: 0.0 since
        # wiki-style narrative content does not carry inline [path|symbol] citations.
        default_threshold = 0.90
        if "citation_coverage_threshold" in self.playbook_metadata:
            try:
                default_threshold = float(self.playbook_metadata["citation_coverage_threshold"])
            except (TypeError, ValueError):
                pass
        self.citation_coverage_threshold = default_threshold
        self.max_citation_rewrite_attempts = 2
        # (self.logger already set above)
        self.min_operations_for_quality = 2
        self.min_touched_files_for_quality = 1
        self.last_run_metrics: dict = {}
        # Actions the playbook mandates must be called at least once before convergence.
        # e.g. documentation playbooks require "write_file" to produce wiki artifacts.
        raw_required = self.playbook_metadata.get("required_actions", []) or []
        self.required_actions: list[str] = (
            list(raw_required) if isinstance(raw_required, (list, tuple)) else []
        )
        # Optional: all write_file paths must start with this prefix (e.g. "docs/wiki/").
        self.required_output_path_prefix: str = (
            str(self.playbook_metadata.get("required_output_path_prefix", "") or "").strip()
        )
        # Minimum number of successful write_file calls required before convergence.
        try:
            self.min_write_file_count: int = int(
                self.playbook_metadata.get("min_write_file_count", 0) or 0
            )
        except (TypeError, ValueError):
            self.min_write_file_count = 0
        # Compliance reminder injected into prompts while required actions are unmet.
        self._required_action_reminder: str | None = None

        # Initialize AutoGen agent with shell tool capability
        self._agent = self._create_autogen_agent()
        self.logger.info(
            "CodeAnalyzer initialized: depth=%s, citation_threshold=%.0f%% (playbook_override=%s)",
            self.analysis_depth_profile,
            self.citation_coverage_threshold * 100,
            "citation_coverage_threshold" in self.playbook_metadata,
        )

    def _create_autogen_agent(self) -> AssistantAgent:
        """Create and configure the AutoGen AssistantAgent without shell tool capability."""
        system_message = self._get_system_message()

        # Create the agent without tools (LLM will provide commands via JSON response)
        agent = AssistantAgent(
            name="code_analyzer",
            system_message=system_message,
            model_client=self.config,
        )

        return agent

    def _get_system_message(self) -> str:
        """Get the system message for the Code Analyzer agent."""
        base_message = r"""You are a Code Analyzer, a technical expert responsible for deep-dive codebase exploration and analysis. Your goal is to provide actionable, evidence-based insights by systematically investigating the source code.

### 🧩 CORE ANALYSIS PROTOCOL
1. **Evidence Over Guesswork**: Never assume how a component works. If you see a call to `process_data()`, you MUST locate and read its definition.
2. **Collaborative Knowledge Base**: You maintain a `key_findings` list. Use it to preserve context across iterations.
3. **Iterative Refinement**: Each turn should build upon the last. Start broad (directories), then go deep (specific files), then synthesize (patterns).

### 🛠️ TOOLSET & SYNTAX
You communicate exclusively via JSON. All interactions with the codebase MUST use these tools:

#### 📂 File System (Read-Only Safety)
- `list_directory`: Map the directory structure. Args: {"path": "."}
- `read_file`: Read source code. Args: {"path": "...", "start_line": 1, "max_lines": 300}
- `search_content`: Regex-based search. Args: {"search_query": "regexPattern", "path": "."}
- `fuzzy_search`: Semantic keyword matching. Args: {"search_query": "keywords", "top_k": 5}

#### 💾 Persistence & Memory (Infinite Content Strategy)
- `write_file`: Create or overwrite a file. Args: {"path": "...", "content": "..."}
- `append_file`: Append content.
- `save_memory`: Save key documentation chunks or complex state to persistent memory (mem0). Args: {"content": "..."}
- `search_memory`: Retrieve previously saved context or documentation chunks. Args: {"search_query": "..."}

#### 🕸️ Structural Graph (Architecture - USE MINIMALLY)
- `query_graph`: Natural language graph search. Use ONLY for high-level structure. Args: {"question": "...", "mode": "bfs"}
- `explain`: Get neighbors/details of a specific component. Args: {"label": "..."}
- `shortest_path`: Trace relationship between two concepts. Args: {"source": "...", "target": "..."}

### 🛑 OPERATIONAL GUARDS
- **Anti-Looping**: If a search or read yields no new info, PIVOT. Do not repeat failed commands or identical graph queries.
- **Minimal Graph Usage**: The structural graph is for orientation. Do NOT spam it. Rely on `read_file` for implementation details.
- **Path Resolution**: If a file isn't found, use `list_directory` on the parent folder. Never guess.
- **Context Depth**: Read files in chunks (max 300 lines) to stay within the observation window.

### 📋 RESPONSE FORMAT
You MUST respond with a single JSON object:
```json
{
    "need_file_operations": true,
    "file_operations": [
        {"action": "read_file", "arguments": {"path": "..."}}
    ],
    "need_graph_query": false,
    "graph_queries": [],
    "key_findings": ["Discovered X", "Module Y handles Z"],
    "current_analysis": "Current status of the investigation...",
    "confidence_level": 5,
    "next_focus_areas": "What you will investigate next..."
}
```
"""
        
        if self.playbook_instructions:
            base_message += f"\n\n🚀 STRATEGIC PLAYBOOK GUIDANCE:\n{self.playbook_instructions}\n"
            base_message += "\nIMPORTANT: The above playbook provides your SPECIFIC TASK and OBJECTIVES. Prioritize these goals while following the core analysis protocol above.\n"

        return base_message

    def analyze_codebase(
        self, query: str, codebase_path: str, specialist_feedback: str | None = None,
        initial_findings: list[str] | None = None
    ) -> str:
        """
        Analyze codebase with multi-round self-iteration for progressive analysis.

        Args:
            query: User's analysis request
            codebase_path: Path to the codebase to analyze
            specialist_feedback: Optional feedback from Task Specialist to guide analysis focus
            initial_findings: Optional pre-discovered findings (e.g. from Graphify report)

        Returns:
            Comprehensive analysis result
        """
        # Initialize iteration state
        max_iterations = self.depth_budgets["max_iterations"]
        current_iteration = 0
        analysis_context = []
        file_operation_history = []
        shared_key_findings = initial_findings or []  # Collaborative knowledge base
        total_file_operations_executed = 0
        total_graph_queries_executed = 0
        memory_results = [] # Results from search_memory for next iteration
        convergence_indicators = {
            "sufficient_code_coverage": False,
            "question_answered": False,
            "confidence_threshold_met": False,
        }
        min_iterations_before_early_stop = 2

        while current_iteration < max_iterations:
            current_iteration += 1
            self.logger.info(
                "Code analyzer iteration %s/%s (depth=%s, file_ops_so_far=%s/%s)",
                current_iteration,
                max_iterations,
                self.analysis_depth_profile,
                total_file_operations_executed,
                self.depth_budgets["max_file_operations_total"],
            )

            # Prepare iteration-specific prompt
            iteration_prompt = self._build_iteration_prompt(
                query,
                codebase_path,
                current_iteration,
                analysis_context,
                file_operation_history,
                shared_key_findings,
                convergence_indicators,
                specialist_feedback,
                memory_results,
            )

            # One LLM turn per iteration; clear AutoGen context so prompts are not duplicated in history
            step_response = run_assistant_single_turn(self._agent, iteration_prompt)

            # Extract text from TaskResult object
            response_text = extract_text_from_autogen_response(step_response)

            # Parse JSON response from LLM
            try:
                import json

                self.logger.debug(f"Raw LLM response: {response_text[:500]}...")

                # Extract JSON from markdown code blocks if present
                json_text = self._extract_json_from_response(response_text)

                llm_decision = json.loads(json_text)
                self.logger.debug(f"Parsed LLM decision: {llm_decision}")

                # Update shared key findings from LLM response
                if "key_findings" in llm_decision:
                    shared_key_findings = llm_decision["key_findings"]
                    
                    # Log knowledge update
                    if self.structured_logger:
                        self.structured_logger.log_knowledge_update(
                            agent="code_analyzer",
                            new_findings=shared_key_findings,
                            confidence_level=llm_decision.get("confidence_level", 0.0),
                            next_investigation_areas=[llm_decision.get("next_focus_areas", "")]
                        )

            except json.JSONDecodeError as e:
                # Fallback: treat as plain text analysis without file system operations
                self.logger.warning(f"JSON parsing failed: {e}")
                self.logger.warning(f"Raw response was: {response_text[:200]}...")
                llm_decision = {
                    "need_file_operations": False,
                    "file_operations": [],
                    "key_findings": shared_key_findings,  # Preserve existing findings
                    "current_analysis": response_text,
                    "confidence_level": 5,
                    "next_focus_areas": "Continue analysis",
                }

            # Keep the analyzer productive: if the model stalls with no actions,
            # inject minimal bootstrap exploration to gather concrete evidence.
            llm_decision = self._inject_bootstrap_exploration_if_needed(
                llm_decision, query, current_iteration, total_file_operations_executed
            )

            # Loop Detection: Check if we are repeating the exact same operations
            current_file_ops = json.dumps(llm_decision.get("file_operations", []), sort_keys=True)
            previous_file_ops = json.dumps(analysis_context[-1].get("llm_decision", {}).get("file_operations", []), sort_keys=True) if analysis_context else ""
            
            current_graph_ops = json.dumps(llm_decision.get("graph_queries", []), sort_keys=True)
            previous_graph_ops = json.dumps(analysis_context[-1].get("llm_decision", {}).get("graph_queries", []), sort_keys=True) if analysis_context else ""

            is_file_loop = current_file_ops != "[]" and current_file_ops == previous_file_ops
            is_graph_loop = current_graph_ops != "[]" and current_graph_ops == previous_graph_ops

            if is_file_loop or is_graph_loop:
                loop_type = "FILE" if is_file_loop else "GRAPH"
                if is_file_loop and is_graph_loop: loop_type = "BOTH FILE and GRAPH"
                
                self.logger.warning(f"REPETITION DETECTED ({loop_type}) at iteration {current_iteration}. Injecting warning.")
                loop_warning = f"\n🛑 CRITICAL WARNING: You are repeating the EXACT same {loop_type} operations as the previous iteration. This indicates you are STUCK. "
                if is_graph_loop:
                    loop_warning += "Stop asking the graph the same questions. Rely on your key findings or use read_file/search_content to verify details in the code."
                else:
                    loop_warning += "Try a different search query, read a different file, or use list_directory to find new paths."
                
                loop_warning += " DO NOT repeat this again."
                
                if specialist_feedback:
                    specialist_feedback += loop_warning
                else:
                    specialist_feedback = loop_warning

                # Proactive Loop Breaking: If repeating, force the agent to pivot by clearing the operations
                self.logger.warning("FORCING PIVOT: Clearing repeated operations to break the loop.")
                llm_decision["file_operations"] = []
                llm_decision["graph_queries"] = []
                llm_decision["need_file_operations"] = False
                llm_decision["need_graph_query"] = False

            # Execute file system operations if needed (execution phase)
            file_results = []
            file_operations = llm_decision.get("file_operations", [])
            
            # Logic Guard: Auto-enable if operations are provided but flag is false
            if file_operations and not llm_decision.get("need_file_operations", False):
                self.logger.warning("Agent provided file_operations but set need_file_operations=false. Auto-enabling.")
                llm_decision["need_file_operations"] = True

            if llm_decision.get("need_file_operations", False):
                remaining_file_budget = self.depth_budgets["max_file_operations_total"] - total_file_operations_executed
                if remaining_file_budget <= 0:
                    self.logger.info("File operation budget exhausted; skipping additional file operations.")
                    llm_decision["need_file_operations"] = False
                    file_operations = []
                else:
                    file_operations = file_operations[: min(self.depth_budgets["max_file_operations_per_iteration"], remaining_file_budget)]
                file_results = self._execute_file_operations(file_operations)
                total_file_operations_executed += len(file_results)
                file_operation_history.append(
                    {
                        "iteration": current_iteration,
                        "commands": file_operations,
                        "results": file_results,
                        "timestamp": self._get_timestamp(),
                    }
                )

            # Execute graph queries if needed
            graph_results = []
            graph_queries = llm_decision.get("graph_queries", [])
            
            # Logic Guard: Auto-enable if queries are provided but flag is false
            if graph_queries and not llm_decision.get("need_graph_query", False):
                self.logger.warning("Agent provided graph_queries but set need_graph_query=false. Auto-enabling.")
                llm_decision["need_graph_query"] = True

            if self.graphify_tool and llm_decision.get("need_graph_query", False):
                # Budget Enforcement: Max 3 graph queries per iteration to prevent spam
                remaining_graph_budget = self.depth_budgets["max_graph_queries_total"] - total_graph_queries_executed
                max_graph_queries = min(3, self.depth_budgets["max_graph_queries_per_iteration"], max(0, remaining_graph_budget))
                if max_graph_queries == 0:
                    self.logger.info("Graph query budget exhausted; skipping graph queries.")
                    graph_queries = []
                if len(graph_queries) > max_graph_queries:
                    self.logger.warning(f"Agent requested {len(graph_queries)} graph queries. Limiting to {max_graph_queries}.")
                    graph_queries = graph_queries[:max_graph_queries]

                self.logger.info(f"Executing {len(graph_queries)} graph queries")
                for query_req in graph_queries:
                    if isinstance(query_req, str):
                        self.logger.warning(f"Expected dict for graph query, got string: {query_req}")
                        graph_results.append({
                            "tool": "unknown",
                            "arguments": {"raw_query": query_req},
                            "output": "Error: Invalid format. Expected JSON object with 'tool' and 'arguments'."
                        })
                        continue
                    tool_name = query_req.get("tool")
                    args = query_req.get("arguments", {})
                    output = self.graphify_tool.execute_tool(tool_name, args)
                    graph_results.append({
                        "tool": tool_name,
                        "arguments": args,
                        "output": output
                    })
                total_graph_queries_executed += len(graph_results)

            # Execute memory operations if needed
            memory_results = []
            memory_ops = llm_decision.get("memory_operations", [])
            if self.memory_tool and llm_decision.get("need_memory", False):
                for op in memory_ops:
                    action = op.get("action")
                    args = op.get("arguments", {})
                    if action == "save_memory":
                        content = args.get("content")
                        if content:
                            self.memory_tool.add_memory(content, user_id=self.session_id)
                            self.logger.info("Saved item to persistent memory")
                    elif action == "search_memory":
                        m_query = args.get("query")
                        if m_query:
                            hits = self.memory_tool.search_memory(m_query, user_id=self.session_id)
                            memory_results.append({
                                "query": m_query,
                                "hits": hits
                            })
                            self.logger.info(f"Memory search for '{m_query}' returned {len(hits)} hits")

            # Store analysis step
            analysis_context.append(
                {
                    "iteration": current_iteration,
                    "llm_decision": llm_decision,
                    "file_results": file_results,
                    "graph_results": graph_results,
                    "timestamp": self._get_timestamp(),
                }
            )

            # Assess convergence based on LLM's confidence and analysis
            convergence_indicators = self._assess_convergence_from_json(
                llm_decision, analysis_context
            )

            # Generate milestone summary at regular intervals
            # Skipped when the playbook sets disable_milestone_summaries: true to save LLM calls.
            milestone_interval = max_iterations // 2  # Two summaries per cycle
            if (
                not getattr(self, "disable_milestone_summaries", False)
                and milestone_interval > 0
                and current_iteration % milestone_interval == 0
            ):
                try:
                    self.logger.info(
                        f"Generating milestone summary at iteration {current_iteration}"
                    )
                    milestone_summary = self._generate_milestone_summary(
                        query,
                        file_operation_history,
                        analysis_context,
                        current_iteration,
                        milestone_interval,
                    )

                    # Add milestone summary to shared knowledge base
                    milestone_number = current_iteration // milestone_interval
                    milestone_finding = f"🔄 MILESTONE {milestone_number} SUMMARY (Iterations {current_iteration-milestone_interval+1}-{current_iteration}): {milestone_summary}"
                    shared_key_findings.append(milestone_finding)

                    self.logger.info(
                        f"Added milestone {milestone_number} summary to knowledge base"
                    )

                except Exception as e:
                    self.logger.warning(f"Failed to generate milestone summary: {e}")

            # Check if analysis is complete
            should_stop_for_convergence = self._should_terminate(convergence_indicators)
            confidence_level = llm_decision.get("confidence_level", 0)
            has_terminal_focus = (
                str(llm_decision.get("next_focus_areas", "")).strip().lower()
                == "final analysis complete"
            )

            # Collect the set of actions used so far across all iterations.
            actions_used: set[str] = {
                r["action"]
                for ctx in analysis_context
                for r in ctx.get("file_results", [])
                if r.get("action")
            }
            missing_required = [
                a for a in self.required_actions if a not in actions_used
            ]
            # Also check path-prefix and minimum write count constraints.
            written_so_far: list[str] = [
                r.get("arguments", {}).get("path", "")
                for ctx in analysis_context
                for r in ctx.get("file_results", [])
                if r.get("action") == "write_file" and r.get("success")
            ]
            prefix = self.required_output_path_prefix
            wrong_paths = (
                [p for p in written_so_far if not p.startswith(prefix)] if prefix else []
            )
            write_count_unmet = len(written_so_far) < self.min_write_file_count
            # Combine all unresolved constraints
            unresolved = bool(missing_required or wrong_paths or write_count_unmet)
            if unresolved:
                parts = []
                if missing_required:
                    parts.append(
                        "use " + ", ".join(f"`{a}`" for a in missing_required) + " at least once"
                    )
                if wrong_paths:
                    parts.append(
                        f"write files ONLY inside `{prefix}` — "
                        f"wrong paths so far: {wrong_paths}"
                    )
                if write_count_unmet:
                    parts.append(
                        f"write at least {self.min_write_file_count} files "
                        f"(written so far: {len(written_so_far)})"
                    )
                self._required_action_reminder = (
                    "\n🔴 PLAYBOOK COMPLIANCE BLOCKER — you cannot stop until:\n"
                    + "\n".join(f"  • {p}" for p in parts)
                    + "\nDo NOT set confidence >= 8 or next_focus_areas='Final analysis complete' "
                    "until ALL of the above are satisfied."
                )
                self.logger.info(
                    "Compliance unresolved: missing_required=%s, wrong_paths=%s, write_count_unmet=%s",
                    missing_required, wrong_paths, write_count_unmet,
                )
            else:
                self._required_action_reminder = None

            can_stop_without_more_ops = (
                not llm_decision.get("need_file_operations", True)
                and current_iteration >= min_iterations_before_early_stop
                and (confidence_level >= 8 or has_terminal_focus)
                and not unresolved  # ← block until all playbook constraints are satisfied
            )
            # Also block the convergence-based stop path while constraints are unresolved.
            if unresolved:
                should_stop_for_convergence = False

            if should_stop_for_convergence or can_stop_without_more_ops:
                break

        # Last-resort recovery: force one deterministic evidence pass if nothing ran.
        if total_file_operations_executed == 0 and total_graph_queries_executed == 0:
            self.logger.warning("No operations executed across all iterations; forcing bootstrap recovery pass.")
            forced_ops = self._build_bootstrap_operations(query)
            forced_results = self._execute_file_operations(forced_ops)
            file_operation_history.append(
                {
                    "iteration": current_iteration + 1,
                    "commands": forced_ops,
                    "results": forced_results,
                    "timestamp": self._get_timestamp(),
                }
            )
            analysis_context.append(
                {
                    "iteration": current_iteration + 1,
                    "llm_decision": {
                        "need_file_operations": True,
                        "file_operations": forced_ops,
                        "need_graph_query": False,
                        "graph_queries": [],
                        "key_findings": shared_key_findings,
                        "current_analysis": "Forced bootstrap evidence collection executed.",
                        "confidence_level": 4,
                        "next_focus_areas": "Synthesize findings from forced bootstrap evidence.",
                    },
                    "file_results": forced_results,
                    "graph_results": [],
                    "timestamp": self._get_timestamp(),
                }
            )
            total_file_operations_executed += len(forced_results)

        # Synthesize final response
        synthesized = self._synthesize_final_response(
            query, analysis_context, shared_key_findings, convergence_indicators
        )
        self.last_run_metrics = self._collect_run_metrics(
            analysis_context, convergence_indicators, synthesized
        )
        if self.last_run_metrics["status"] == "insufficient_evidence":
            return self._render_insufficient_evidence_report(query, self.last_run_metrics)
        return synthesized

    def _execute_file_operations(self, operations: list[dict]) -> list[dict]:
        """Execute a list of structured file operations and return results."""
        results = []
        for op in operations:
            if isinstance(op, str):
                self.logger.warning(f"Expected dict for file operation, got string: {op}")
                results.append({
                    "action": "unknown",
                    "arguments": {"raw_command": op},
                    "success": False,
                    "stdout": "",
                    "stderr": "",
                    "error": "Invalid format: Expected JSON object with 'action' and 'arguments', got string.",
                })
                continue
                
            action = op.get("action", "unknown")
            arguments = op.get("arguments", {})
            try:
                # Handle standard file system operations
                if action in ["list_directory", "read_file", "search_content", "fuzzy_search", "write_file", "append_file"]:
                    success, stdout, stderr = self.file_system_tool.execute_operation(action, arguments)
                
                # Handle mem0 memory operations
                elif action == "save_memory" and self.memory_tool:
                    content = arguments.get("content", "")
                    success = self.memory_tool.add_memory(content, user_id=self.session_id)
                    stdout = "Memory saved successfully." if success else "Failed to save memory."
                    stderr = ""
                
                elif action == "search_memory" and self.memory_tool:
                    query = arguments.get("search_query") or arguments.get("query", "")
                    results = self.memory_tool.search_memory(query, user_id=self.session_id)
                    success = True
                    stdout = str(results)
                    stderr = ""
                
                else:
                    success = False
                    stdout = ""
                    stderr = f"Unknown action: {action} or memory tool not initialized."

                result = {
                    "action": action,
                    "arguments": arguments,
                    "success": success,
                    "stdout": stdout or "",
                    "stderr": stderr or "",
                    "error": None,
                }
                
                # Log to structured session logs
                if self.structured_logger:
                    self.structured_logger.log_command_executed(
                        agent="code_analyzer",
                        command=f"{action}({arguments})",
                        exit_code=0 if success else 1,
                        output_size=len(stdout or "") + len(stderr or "")
                    )
            except Exception as e:
                result = {
                    "action": action,
                    "arguments": arguments,
                    "success": False,
                    "stdout": "",
                    "stderr": "",
                    "error": str(e),
                }
            results.append(result)

        return results

    def _assess_convergence_from_json(self, llm_decision: dict, context: list) -> dict:
        """Assess convergence based on LLM's JSON response."""
        convergence = {
            "sufficient_code_coverage": False,
            "question_answered": False,
            "confidence_threshold_met": False,
        }

        # Check confidence level from LLM
        confidence = llm_decision.get("confidence_level", 0)
        if confidence >= 8:
            convergence["confidence_threshold_met"] = True

        # Check if LLM indicates no need for more shell execution
        if not llm_decision.get("need_file_operations", True):
            convergence["question_answered"] = True

        # Check for code coverage based on breadth of visited files + executed operations.
        total_commands = sum(len(ctx.get("file_results", [])) for ctx in context)
        touched_files: set[str] = set()
        for ctx in context:
            for result in ctx.get("file_results", []):
                args = result.get("arguments", {})
                if isinstance(args, dict):
                    file_path = args.get("path")
                    if isinstance(file_path, str) and file_path:
                        touched_files.add(file_path)

        if total_commands >= 4 and (len(touched_files) >= 3 or len(context) >= 3):
            convergence["sufficient_code_coverage"] = True

        return convergence

    def _generate_milestone_summary(
        self,
        query: str,
        shell_history: list,
        analysis_context: list,
        current_iteration: int,
        milestone_interval: int,
    ) -> str:
        """
        Generate a comprehensive milestone summary of recent iterations.

        Args:
            query: The original user query
            shell_history: Complete shell execution history
            analysis_context: Complete analysis context
            current_iteration: Current iteration number
            milestone_interval: Interval between milestones

        Returns:
            Comprehensive summary string
        """
        # Calculate the range of iterations to summarize
        start_iteration = max(1, current_iteration - milestone_interval + 1)
        end_iteration = current_iteration

        # Filter relevant history for this milestone period
        relevant_shell_history = [
            sh
            for sh in shell_history
            if start_iteration <= sh["iteration"] <= end_iteration
        ]

        relevant_analysis_context = [
            ctx
            for ctx in analysis_context
            if start_iteration <= ctx["iteration"] <= end_iteration
        ]

        # Build comprehensive summary prompt
        summary_prompt = f"""
        You are tasked with creating a MILESTONE SUMMARY for codebase analysis iterations {start_iteration}-{end_iteration}.

        Original Query: {query}

        Your goal is to synthesize ALL discoveries, patterns, and insights from these {milestone_interval} iterations into a comprehensive summary that preserves critical knowledge for future iterations.

        === SHELL EXECUTION HISTORY FOR THIS MILESTONE ===
        """

        for shell_exec in relevant_shell_history:
            summary_prompt += f"\nIteration {shell_exec['iteration']}:\n"
            for result in shell_exec["results"]:
                summary_prompt += f"Action: {result['action']} {result['arguments']}\n"
                if result["success"] and result.get("stdout"):
                    # Include more complete output for summary purposes
                    stdout_sample = result["stdout"][:800]  # More context for summary
                    summary_prompt += f"Output: {stdout_sample}...\n"
                else:
                    summary_prompt += f"Error: {result['stderr'] or result.get('error', 'Unknown error')}\n"
            summary_prompt += "\n"

        summary_prompt += "\n=== ANALYSIS INSIGHTS FOR THIS MILESTONE ===\n"

        for ctx in relevant_analysis_context:
            iteration = ctx["iteration"]
            llm_decision = ctx.get("llm_decision", {})

            summary_prompt += f"\nIteration {iteration}:\n"

            current_analysis = llm_decision.get("current_analysis", "")
            if current_analysis:
                summary_prompt += f"Analysis: {current_analysis}\n"

            focus_areas = llm_decision.get("next_focus_areas", "")
            if focus_areas:
                summary_prompt += f"Focus Areas: {focus_areas}\n"

            confidence = llm_decision.get("confidence_level", "N/A")
            summary_prompt += f"Confidence: {confidence}\n"

        summary_prompt += """

        === SUMMARY REQUIREMENTS ===
        Create a comprehensive milestone summary that captures:

        1. **Key Technical Discoveries**: What specific technical details were uncovered?
        2. **Architectural Patterns**: What structural or design patterns were identified?
        3. **Important Files/Components**: Which files or components are most significant?
        4. **Relationships & Dependencies**: How do different parts connect or depend on each other?
        5. **Configuration & Setup**: Any important configuration or setup insights?
        6. **Progress Assessment**: What has been thoroughly understood vs. what needs more investigation?
        7. **Critical Insights**: Any breakthrough understanding or important realizations?

        Format as a dense, information-rich summary that future iterations can build upon.
        Focus on concrete technical findings rather than process descriptions.

        Keep it concise but comprehensive - aim for 3-5 sentences that capture the essence of all discoveries.
        """

        try:
            summary = extract_text_from_autogen_response(
                run_assistant_single_turn(self._agent, summary_prompt)
            )

            # Clean and validate the summary
            if summary and len(summary.strip()) > 20:
                return summary.strip()
            else:
                # Fallback summary if LLM fails
                return f"Milestone {current_iteration//milestone_interval} completed iterations {start_iteration}-{end_iteration}. Executed {len(relevant_shell_history)} file operation sessions with focus on {query}."

        except Exception as e:
            self.logger.warning(f"LLM summary generation failed: {e}")
            # Simple fallback summary
            return f"Milestone summary for iterations {start_iteration}-{end_iteration}: Executed {len(relevant_shell_history)} file operation sessions analyzing {query}."

    def _build_iteration_prompt(
        self,
        query: str,
        codebase_path: str,
        iteration: int,
        context: list,
        shell_history: list,
        shared_key_findings: list,
        convergence: dict,
        specialist_feedback: str | None = None,
        memory_results: list | None = None,
    ) -> str:
        """Build unified prompt with shared knowledge base for progressive analysis."""

        base_prompt = f"""
        CODEBASE ANALYSIS - ITERATION {iteration}

        Target: {codebase_path}
        User Query: {query}

        ULTIMATE GOAL: Create a comprehensive, detailed report that thoroughly addresses the user's query.
        Your final deliverable should be a well-structured analysis that provides actionable insights and complete answers.

        ACTIVE DEPTH PROFILE: {self.analysis_depth_profile.upper()}
        OPERATION BUDGETS (hard limits):
        - max_iterations: {self.depth_budgets["max_iterations"]}
        - max_file_operations_total: {self.depth_budgets["max_file_operations_total"]}
        - max_file_operations_per_iteration: {self.depth_budgets["max_file_operations_per_iteration"]}
        - max_graph_queries_total: {self.depth_budgets["max_graph_queries_total"]}
        - max_graph_queries_per_iteration: {self.depth_budgets["max_graph_queries_per_iteration"]}
        IMPORTANT: Prioritize highest-value evidence first; you cannot exceed these budgets.

        ANALYSIS STRATEGY GUIDANCE:
        You are encouraged to follow a progressive analysis approach, but you have full autonomy to decide your exploration strategy based on the specific query and context:

        🎯 SUGGESTED PROGRESSION (adapt as needed):
        1. TARGETED EXPLORATION: Start with specific, query-related areas (find relevant files, grep keywords)
        2. CONTEXTUAL EXPANSION: Explore related files, dependencies, configurations around your findings
        3. DEEPER ANALYSIS: Read actual code content, understand implementation details and algorithms
        4. COMPREHENSIVE COVERAGE: Fill gaps, check alternatives, verify understanding
        5. VALIDATION & SYNTHESIS: Double-check findings, resolve inconsistencies, provide final analysis

        💡 STRATEGIC CONSIDERATIONS:
        - For simple queries: You might jump directly to targeted searches and provide quick answers
        - For complex queries: Follow the full progression to ensure comprehensive coverage
        - For architectural questions: Focus on structure, relationships, and high-level patterns
        - For implementation questions: Dive deep into specific code logic and details

        🔧 RECOMMENDED FILE OPERATIONS:
        - Exploration: "list_directory"
        - Search: "search_content"
        - Content: "read_file"
        - Analysis: "read_file" (view structure)

        Remember: You must respond in valid JSON format with the exact structure specified in your system message.

        """

        # Add specialist feedback if provided
        if specialist_feedback:
            base_prompt += f"""
        🎯 TASK SPECIALIST FEEDBACK - PRIORITY FOCUS AREAS:
        {specialist_feedback}

        IMPORTANT: Address the above feedback areas as your primary focus. The Task Specialist has identified
        these as critical gaps in the previous analysis. Make sure to specifically target these areas in your
        exploration strategy.

        """

        # Inject playbook compliance reminder when required actions have not yet been used.
        # This overrides any premature confidence signal from the LLM.
        if getattr(self, "_required_action_reminder", None):
            base_prompt += f"""
        {self._required_action_reminder}

        """

        # Add shared knowledge base (collaborative key findings)
        if shared_key_findings:
            base_prompt += (
                "\n🧠 SHARED KNOWLEDGE BASE (Key Findings from All Iterations):\n"
            )
            for i, finding in enumerate(shared_key_findings, 1):
                base_prompt += f"{i}. {finding}\n"
            base_prompt += (
                "\nYou can ADD, UPDATE, REFINE, or REMOVE findings in your response.\n"
            )
        else:
            base_prompt += "\n🧠 SHARED KNOWLEDGE BASE: Empty (you'll create the first key findings)\n"

        # Add recent shell execution results for context
        if shell_history:
            base_prompt += "\n📋 RECENT SHELL EXECUTION RESULTS:\n"
            for shell_exec in shell_history[-5:]:  # Show last 5 executions (Long-range memory)
                base_prompt += f"\nIteration {shell_exec['iteration']}:\n"
                for result in shell_exec["results"]:
                    base_prompt += f"Action: {result['action']} {result['arguments']}\n"
                    if result["success"]:
                        # Expanded observation window for deep code reading
                        stdout_preview = (
                            result["stdout"][:2500] + "..."
                            if len(result["stdout"]) > 2500
                            else result["stdout"]
                        )
                        base_prompt += f"Output: {stdout_preview}\n"
                    else:
                        base_prompt += f"Error: {result['stderr'] or result.get('error', 'Unknown error')}\n"
                base_prompt += "\n"

        # Add brief recent analysis context (not full history)
        if context:
            base_prompt += "\n📊 RECENT ANALYSIS CONTEXT:\n"
            for ctx in context[-1:]:  # Show only last context
                llm_decision = ctx.get("llm_decision", {})
                base_prompt += f"Previous iteration {ctx['iteration']} focused on: {llm_decision.get('next_focus_areas', 'N/A')}\n"
            
            # Add recent graph results if any
            if ctx.get("graph_results"):
                base_prompt += "\n📊 RECENT GRAPH QUERY RESULTS:\n"
                for g_res in ctx["graph_results"]:
                    base_prompt += f"Tool: {g_res['tool']}({g_res['arguments']})\n"
                    base_prompt += f"Output: {str(g_res['output'])[:1500]}...\n"

        # Add memory search results from previous iteration
        if memory_results:
            base_prompt += "\n🧠 RECENT MEMORY SEARCH RESULTS:\n"
            for m_res in memory_results:
                base_prompt += f"Query: {m_res['query']}\n"
                for i, hit in enumerate(m_res['hits'], 1):
                    base_prompt += f"  Hit {i}: {hit['memory']}\n"
            base_prompt += "\n"


        # Add current iteration context and convergence status
        base_prompt += f"""

        📈 CURRENT ANALYSIS STATUS:
        - Iteration: {iteration}/10
        - Code coverage sufficient: {convergence['sufficient_code_coverage']}
        - Question answered: {convergence['question_answered']}
        - Confidence threshold met: {convergence['confidence_threshold_met']}

        🎯 DECISION POINTS FOR THIS ITERATION:
        Based on the shared knowledge base and your analysis so far, decide:
        1. Do you need more information via file system operations? (set need_file_operations: true/false)
        2. What specific file operations would help you gather the missing information?
        3. What is your current confidence level (1-10) in providing a comprehensive answer?
        4. If confidence >= 8, consider providing your final comprehensive analysis

        ⚠️ CRITICAL: Update the "key_findings" list:
        - ADD new important discoveries from this iteration
        - UPDATE or REFINE existing findings with new insights
        - REMOVE findings that are no longer relevant or incorrect
        - Keep findings concise but informative (1-2 sentences each)

        This shared knowledge base is the collective memory of all iterations.

        RESPONSE FORMAT: You MUST respond in valid JSON format with these exact fields:
        {
            "need_file_operations": true/false,
            "file_operations": [{"action": "list_directory", "arguments": {"path": "."}}],
            "need_graph_query": true/false,
            "graph_queries": [{"tool": "query_graph", "arguments": {"question": "..."}}],
            "need_memory": true/false,
            "memory_operations": [
                {"action": "save_memory", "arguments": {"content": "..."}},
                {"action": "search_memory", "arguments": {"query": "..."}}
            ],
            "key_findings": ["Updated list of key findings from all iterations"],
            "current_analysis": "Your analysis of this iteration and current understanding",
            "confidence_level": 1-10,
            "next_focus_areas": "What you plan to focus on next (or 'Final analysis complete' if done)"
        }
        """

        return base_prompt

    def _should_terminate(self, convergence: dict) -> bool:
        """Determine if analysis should terminate based on convergence indicators."""
        # Terminate if all convergence criteria are met
        return all(convergence.values())

    def _synthesize_final_response(
        self, query: str, context: list, shared_key_findings: list, convergence: dict
    ) -> str:
        """Synthesize final comprehensive response from shared knowledge base and iterations."""
        if not context:
            return "No analysis performed."

        # Get the most recent analysis
        final_context = context[-1]
        final_decision = final_context.get("llm_decision", {})
        final_confidence = final_decision.get("confidence_level", 0)

        # Create comprehensive synthesis with KEY FINDINGS and proper final analysis
        synthesis = f"""
        CODEBASE ANALYSIS COMPLETE

        Query: {query}
        Iterations: {len(context)}
        Final Confidence Level: {final_confidence}/10
        Convergence Status: {convergence}

        KEY FINDINGS (Collaborative Knowledge Base):
        """

        # Add key findings for debugging and transparency
        if shared_key_findings:
            for i, finding in enumerate(shared_key_findings, 1):
                synthesis += f"{i}. {finding}\n"
        else:
            synthesis += "No key findings available.\n"

        # Generate comprehensive final analysis from all findings
        synthesis += """

        FINAL ANALYSIS:
        """

        if shared_key_findings:
            # Create a comprehensive technical report based on all key findings
            synthesis += self._generate_comprehensive_analysis(
                query, shared_key_findings, context
            )
        else:
            synthesis += (
                "Unable to perform comprehensive analysis due to insufficient findings."
            )

        synthesis += """

        EXECUTION SUMMARY:
        """

        # Add execution summary
        for ctx in context:
            iteration = ctx["iteration"]
            file_results = ctx.get("file_results", [])
            llm_decision = ctx.get("llm_decision", {})

            synthesis += f"\n--- Iteration {iteration} ---\n"
            synthesis += f"Actions executed: {len(file_results) + len(ctx.get('graph_results', []))}\n"
            
            if file_results:
                for result in file_results:
                    status = "✓" if result["success"] else "✗"
                    synthesis += f"  {status} {result['action']} {result['arguments']}\n"
            
            if ctx.get("graph_results"):
                for g_res in ctx["graph_results"]:
                    synthesis += f"  📊 Graph Query: {g_res['tool']}({g_res['arguments']})\n"

            synthesis += f"Confidence: {llm_decision.get('confidence_level', 'N/A')}\n"

            # Show knowledge base growth
            kb_size = len(llm_decision.get("key_findings", []))
            synthesis += f"Knowledge base size: {kb_size} findings\n"

        return synthesis

    def _collect_run_metrics(self, context: list, convergence: dict, report: str) -> dict:
        """Collect quality-oriented metrics from a completed run."""
        total_file_ops = sum(len(ctx.get("file_results", [])) for ctx in context)
        total_graph_ops = sum(len(ctx.get("graph_results", [])) for ctx in context)
        total_actions = total_file_ops + total_graph_ops
        touched_files: set[str] = set()
        for ctx in context:
            for result in ctx.get("file_results", []):
                args = result.get("arguments", {})
                if isinstance(args, dict):
                    p = args.get("path")
                    if isinstance(p, str) and p:
                        touched_files.add(p)
        final_confidence = 0
        if context:
            final_confidence = float(context[-1].get("llm_decision", {}).get("confidence_level", 0) or 0)
        protocol_leak_detected = bool(
            re.search(r"<\|channel\|>|to=\w+|repo_browser\.", report, re.IGNORECASE)
        )
        evidence_warning_detected = "EVIDENCE CHECK WARNING" in report
        insufficient = (
            total_actions < self.min_operations_for_quality
            or len(touched_files) < self.min_touched_files_for_quality
        )
        # Determine which playbook-required actions were actually executed.
        actions_used: set[str] = {
            r["action"]
            for ctx in context
            for r in ctx.get("file_results", [])
            if r.get("action")
        }
        missing_required_actions = [
            a for a in self.required_actions if a not in actions_used
        ]
        # Collect paths of successfully written files.
        written_paths: list[str] = [
            r.get("arguments", {}).get("path", "")
            for ctx in context
            for r in ctx.get("file_results", [])
            if r.get("action") == "write_file" and r.get("success")
        ]
        # Validate path-prefix requirement.
        prefix = self.required_output_path_prefix
        wrong_path_writes: list[str] = (
            [p for p in written_paths if not p.startswith(prefix)]
            if prefix else []
        )
        # Validate minimum write count.
        write_count_ok = len(written_paths) >= self.min_write_file_count
        return {
            "status": "insufficient_evidence" if insufficient else "ok",
            "total_file_ops": total_file_ops,
            "total_graph_ops": total_graph_ops,
            "total_actions": total_actions,
            "touched_files": sorted(touched_files),
            "touched_file_count": len(touched_files),
            "final_confidence": final_confidence,
            "convergence": convergence,
            "protocol_leak_detected": protocol_leak_detected,
            "evidence_warning_detected": evidence_warning_detected,
            "actions_used": sorted(actions_used),
            "missing_required_actions": missing_required_actions,
            "written_paths": written_paths,
            "wrong_path_writes": wrong_path_writes,
            "write_count_ok": write_count_ok,
            "required_output_path_prefix": prefix,
            "min_write_file_count": self.min_write_file_count,
        }

    def _render_insufficient_evidence_report(self, query: str, metrics: dict) -> str:
        """Return a concise, explicit failure report when no meaningful exploration happened."""
        return (
            "CODEBASE ANALYSIS FAILED\n\n"
            f"Query: {query}\n"
            "Reason: insufficient_evidence\n"
            f"Actions executed: {metrics.get('total_actions', 0)} "
            f"(file={metrics.get('total_file_ops', 0)}, graph={metrics.get('total_graph_ops', 0)})\n"
            f"Touched files: {metrics.get('touched_file_count', 0)}\n"
            "Retry guidance: rerun with targeted file operations (`list_directory`, `search_content`, `read_file`) "
            "before synthesis."
        )

    def _generate_comprehensive_analysis(
        self, query: str, key_findings: list, context: list
    ) -> str:
        """Generate a comprehensive technical analysis report from complete analysis context."""
        try:
            # Build a comprehensive prompt using ALL available information
            synthesis_prompt = f"""
            Based on the complete codebase analysis process, generate a comprehensive technical report that answers the user's query: "{query}"

            === ANALYSIS CONTEXT ===

            Key Findings Summary:
            """

            for i, finding in enumerate(key_findings, 1):
                synthesis_prompt += f"{i}. {finding}\n"

            synthesis_prompt += "\n=== DETAILED ANALYSIS ITERATIONS ===\n"

            # Include analysis from each iteration for richer context
            for ctx in context:
                iteration = ctx.get("iteration", "Unknown")
                llm_decision = ctx.get("llm_decision", {})
                file_results = ctx.get("file_results", [])

                synthesis_prompt += f"\nIteration {iteration}:\n"

                # Add file system operation insights
                if file_results:
                    synthesis_prompt += "Actions executed and key discoveries:\n"
                    for result in file_results:
                        if result.get("success") and result.get("stdout"):
                            # Include relevant command output (truncated)
                            stdout_sample = result["stdout"][:500]
                            synthesis_prompt += (
                                f"- {result['action']} {result['arguments']}: {stdout_sample}...\n"
                            )

                # Add LLM analysis from this iteration
                current_analysis = llm_decision.get("current_analysis", "")
                if current_analysis:
                    synthesis_prompt += f"Analysis insights: {current_analysis}\n"

                # Add focus areas
                focus_areas = llm_decision.get("next_focus_areas", "")
                if focus_areas:
                    synthesis_prompt += f"Focus areas identified: {focus_areas}\n"

            # If a playbook is active, try to extract its specific output schema
            custom_schema = None
            if self.playbook_instructions:
                # Look for the Output Schema marker in the instructions
                schema_marker = "### TARGET OUTPUT STRUCTURE"
                if schema_marker in self.playbook_instructions:
                    # Extract everything after the marker
                    custom_schema = self.playbook_instructions.split(schema_marker)[-1].strip()

            if custom_schema:
                synthesis_prompt += f"""
            
            === TARGET OUTPUT SCHEMA ===
            You MUST format your final response based on this specific schema from the playbook:
            {custom_schema}
            
            Ensure ALL fields required by this schema are populated with evidence found during iterations.
            """
            else:
                # Check if the active playbook targets documentation generation.
                is_doc_playbook = bool(
                    self.playbook_instructions
                    and any(
                        kw in (self.playbook_instructions + (self.playbook_metadata.get("name", "") or "")).lower()
                        for kw in ("doc-generation", "wiki", "documentation generation", "comprehensive wiki")
                    )
                )

                if is_doc_playbook:
                    synthesis_prompt += """

            === DOCUMENTATION SYNTHESIS REQUIREMENTS ===
            Your output IS the final wiki document — it will be returned directly in the API response.
            Produce a COMPLETE, renderable Markdown wiki with ALL of the following sections:

            # <Codebase Name> — Developer Wiki

            ## 1. System Overview
            - Architecture pattern (monolith / microservices / event-driven / etc.)
            - High-level module index (table with Module | Purpose | Key Files)
            - System context diagram (Mermaid)

            ## 2. Module Deep-Dives
            For each major module discovered, produce a `### <Module Name>` section containing:
            - 2-3 sentence overview
            - Internal architecture diagram (Mermaid)
            - Component catalog: every key class/function with signature, purpose, parameters, return type
            - Key source snippet (actual code, not pseudocode)
            - External dependencies

            ## 3. API & Integration Reference
            - All public endpoints / CLI commands (table: Method | Path | Description | Auth)
            - External service integrations (table: Service | Purpose | Config Key)

            ## 4. Data Flow
            End-to-end request/event lifecycle (Mermaid flowchart from entry to persistence)

            ## 5. Build, Test & Tooling
            - Build pipeline steps
            - Test strategy and key test locations
            - Linting / formatting toolchain

            RULES:
            - Return ONLY the Markdown document — no preamble, no "here is your wiki" wrapper.
            - Every architectural claim must be backed by a specific file path you actually read.
            - All Mermaid diagrams must use valid syntax (graph TD, flowchart LR, sequenceDiagram, etc.).
            - Use real names, paths, and signatures from your findings — no placeholders.
            """
                else:
                    synthesis_prompt += """

            === SYNTHESIS REQUIREMENTS ===
            Create a comprehensive technical report that:
            1. Directly answers the user's query with specific technical details
            2. Synthesizes information from all iterations into coherent sections
            3. Provides concrete examples from the actual codebase analysis
            4. Explains component relationships and architectural patterns
            5. Includes specific file paths, class names, method signatures discovered
            6. Identifies key integration points and technical patterns
            7. Highlights important technical considerations for implementation

            Format as a clear, actionable technical report that an engineer could use immediately.
            Focus on technical substance, not process meta-information.
            """

            synthesis_prompt += """

            === EVIDENCE CITATION REQUIREMENT (MANDATORY) ===
            For EVERY claim, include an inline citation in this exact format:
            [path: <relative/file/path> | symbol: <class/function/method/config key>]
            Example:
            "Authentication middleware validates JWT in the request pipeline [path: src/auth/middleware.py | symbol: AuthMiddleware.validate_token]"
            If you cannot cite a concrete file+symbol, do not make the claim.
            """

            comprehensive_analysis = extract_text_from_autogen_response(
                run_assistant_single_turn(self._agent, synthesis_prompt)
            )
            comprehensive_analysis = self._strip_internal_protocol_markers(comprehensive_analysis)

            if comprehensive_analysis and len(comprehensive_analysis.strip()) > 50:
                validated_report = self._enforce_evidence_citation_checks(
                    comprehensive_analysis.strip(), query, key_findings, context
                )
                return validated_report
            else:
                # If LLM synthesis fails, return basic information without fake intelligence
                return "LLM synthesis failed. Raw key findings:\n" + "\n".join(
                    f"- {finding}" for finding in key_findings
                )

        except Exception as e:
            self.logger.warning(f"Failed to generate comprehensive analysis: {e}")
            return (
                f"Analysis synthesis failed due to error: {e}\nRaw key findings:\n"
                + "\n".join(f"- {finding}" for finding in key_findings)
            )

    def _build_bootstrap_operations(self, query: str) -> list[dict]:
        """Deterministic minimum exploration plan for code-level tasks."""
        tokens = [t for t in re.findall(r"[A-Za-z0-9_]+", query or "") if len(t) >= 3][:6]
        search_pattern = "|".join(re.escape(t) for t in tokens) if tokens else "main|app|server|config|auth"
        return [
            {"action": "list_directory", "arguments": {"path": "."}},
            {"action": "search_content", "arguments": {"search_query": search_pattern, "path": "."}},
            {"action": "fuzzy_search", "arguments": {"search_query": query or "entrypoint", "top_k": 5}},
        ]

    def _inject_bootstrap_exploration_if_needed(
        self,
        llm_decision: dict,
        query: str,
        current_iteration: int,
        total_file_operations_executed: int,
    ) -> dict:
        """Inject fallback file operations when the LLM provides no actionable plan."""
        has_file_ops = bool(llm_decision.get("file_operations"))
        needs_file_ops = bool(llm_decision.get("need_file_operations", False))
        if has_file_ops or needs_file_ops:
            return llm_decision
        if total_file_operations_executed > 0:
            return llm_decision
        if current_iteration > 2:
            return llm_decision
        bootstrap_ops = self._build_bootstrap_operations(query)
        llm_decision["need_file_operations"] = True
        llm_decision["file_operations"] = bootstrap_ops
        llm_decision["current_analysis"] = (
            str(llm_decision.get("current_analysis", "")).strip()
            + "\n[Bootstrap] Injected deterministic exploration due to missing file operations."
        ).strip()
        return llm_decision

    def _strip_internal_protocol_markers(self, text: str) -> str:
        """Remove accidental internal tool/protocol artifacts from model output."""
        if not isinstance(text, str) or not text.strip():
            return text
        cleaned = []
        for ln in text.splitlines():
            if re.search(r"<\|channel\|>|to=\w+|repo_browser\.", ln, re.IGNORECASE):
                continue
            cleaned.append(ln)
        return "\n".join(cleaned).strip()

    def _get_timestamp(self) -> str:
        """Get current timestamp for logging."""
        import datetime

        return datetime.datetime.now().isoformat()

    def _get_depth_budgets(self, profile: str) -> dict:
        """Return hard operation budgets per depth profile."""
        mi = DEPTH_PROFILE_MAX_ITERATIONS
        profiles = {
            "quick": {
                "max_iterations": mi["quick"],
                "max_file_operations_total": 18,
                "max_file_operations_per_iteration": 4,
                "max_graph_queries_total": 6,
                "max_graph_queries_per_iteration": 2,
            },
            "standard": {
                "max_iterations": mi["standard"],
                "max_file_operations_total": 40,
                "max_file_operations_per_iteration": 6,
                "max_graph_queries_total": 12,
                "max_graph_queries_per_iteration": 3,
            },
            # doc: fewer iterations but more ops/iteration so the agent can batch reads
            # efficiently. Graph queries disabled — the wiki is built from file content.
            "doc": {
                "max_iterations": mi["doc"],
                "max_file_operations_total": 48,
                "max_file_operations_per_iteration": 8,
                "max_graph_queries_total": 0,
                "max_graph_queries_per_iteration": 0,
            },
            "deep": {
                "max_iterations": mi["deep"],
                "max_file_operations_total": 80,
                "max_file_operations_per_iteration": 8,
                "max_graph_queries_total": 20,
                "max_graph_queries_per_iteration": 4,
            },
            "forensic": {
                "max_iterations": mi["forensic"],
                "max_file_operations_total": 130,
                "max_file_operations_per_iteration": 10,
                "max_graph_queries_total": 35,
                "max_graph_queries_per_iteration": 5,
            },
        }
        return profiles.get(profile, profiles["standard"])

    def _has_required_evidence_citation(self, text: str) -> bool:
        """Check whether a claim includes [path: ... | symbol: ...] citation."""
        pattern = r"\[path:\s*[^\]|]+\s*\|\s*symbol:\s*[^\]]+\]"
        return bool(re.search(pattern, text))

    def _enforce_evidence_citation_checks(
        self, report: str, query: str, key_findings: list, context: list
    ) -> str:
        """
        Ensure final answer claims include file path + symbol citations.
        Enforces minimum citation coverage and retries rewrites when unmet.
        """
        def extract_claim_lines(text: str) -> list[str]:
            return [
                ln.strip()
                for ln in text.splitlines()
                if ln.strip() and not ln.strip().startswith("#")
            ]

        def coverage_for(text: str) -> tuple[float, int, int]:
            claim_lines = extract_claim_lines(text)
            eligible = [ln for ln in claim_lines if len(ln) > 30]
            if not eligible:
                return 1.0, 0, 0
            cited = [ln for ln in eligible if self._has_required_evidence_citation(ln)]
            return len(cited) / len(eligible), len(cited), len(eligible)

        best_report = report
        best_coverage, _, _ = coverage_for(best_report)
        if best_coverage >= self.citation_coverage_threshold:
            return best_report

        rewrite_template = """
Rewrite the report below so EVERY technical claim has this inline citation format:
[path: <relative/file/path> | symbol: <class/function/method/config key>]

Rules:
1) Do not add claims that are not supported by known findings.
2) If a claim lacks verifiable evidence, remove it.
3) Keep the same overall structure and answer the original query.
4) Return only the rewritten report text.
5) Target minimum citation coverage of {threshold:.0%}.

Original query: {query}

Known findings:
{findings}

Report to rewrite:
{report}
"""
        try:
            for _ in range(self.max_citation_rewrite_attempts):
                rewrite_prompt = rewrite_template.format(
                    threshold=self.citation_coverage_threshold,
                    query=query,
                    findings=chr(10).join(f"- {f}" for f in key_findings[:40]),
                    report=best_report,
                )
                rewritten = extract_text_from_autogen_response(
                    run_assistant_single_turn(self._agent, rewrite_prompt)
                ).strip()
                if not rewritten:
                    break
                rewritten_coverage, _, _ = coverage_for(rewritten)
                if rewritten_coverage > best_coverage:
                    best_report = rewritten
                    best_coverage = rewritten_coverage
                if rewritten_coverage >= self.citation_coverage_threshold:
                    return rewritten
        except Exception as e:
            self.logger.warning(f"Evidence citation rewrite failed: {e}")

        final_coverage, cited_count, total_count = coverage_for(best_report)
        return best_report + (
            "\n\nEVIDENCE CHECK WARNING:\n"
            f"Citation coverage is {final_coverage:.0%} ({cited_count}/{total_count}), "
            f"below required {self.citation_coverage_threshold:.0%}. "
            "Re-run with a deeper profile or reduce unsupported claims."
        )

    def _extract_json_from_response(self, response_text: str) -> str:
        """Extract JSON content from LLM response, handling markdown code blocks."""
        import re

        # Try to find JSON within markdown code blocks
        json_pattern = r"```(?:json)?\s*(\{.*?\})\s*```"
        matches = re.findall(json_pattern, response_text, re.DOTALL)

        if matches:
            # Use the first JSON block found
            json_content = matches[0].strip()
            self.logger.debug(f"Extracted JSON from markdown: {json_content[:200]}...")
            return json_content

        # If no markdown blocks, check if the response starts/ends with braces
        stripped = response_text.strip()
        if stripped.startswith("{") and stripped.endswith("}"):
            self.logger.debug("Found JSON-like content without markdown")
            return stripped

        # Last resort: try to find JSON pattern in the text
        json_like_pattern = r"\{[^{}]*(?:\{[^{}]*\}[^{}]*)*\}"
        json_matches = re.findall(json_like_pattern, response_text, re.DOTALL)

        if json_matches:
            # Try to find the most complete JSON (longest match)
            longest_match = max(json_matches, key=len)
            self.logger.debug(f"Found JSON-like pattern: {longest_match[:200]}...")
            return longest_match

        # If no JSON found, return original text and let JSON parser fail
        self.logger.warning("No JSON pattern found in response")
        return response_text

    @property
    def agent(self) -> AssistantAgent:
        """Get the underlying AutoGen agent."""
        return self._agent
