"""
Agent Manager for orchestrating multi-agent codebase analysis.

This module implements the central orchestration layer that coordinates
Code Analyzer and Task Specialist through a review cycle mechanism.
"""

import logging
import os
import re
from typing import Any, Optional
from functools import wraps

from ..config.configuration import ConfigurationManager
from ..tools.file_system_tool import FileSystemTool
from .code_analyzer import CodeAnalyzer, iteration_cap_for_depth_profile
from .task_specialist import TaskSpecialist
from ..utils.playbook import PlaybookManager, Playbook
from ..utils.graphify_cli import GraphifyCLI
from ..tools.graphify_tool import GraphifyTool
from ..utils.logging import setup_logging, get_structured_logger
from ..tools.memory_tool import MemoryTool


def infer_analysis_depth_from_query(query: str) -> str:
    """
    Choose quick/standard/deep/forensic from task wording.

    Kept conservative: common words like \"architecture\" alone no longer force
    \"deep\" mode (that profile roughly doubles LLM iterations vs standard).
    """
    q = (query or "").lower()
    if not q:
        return "standard"

    quick_patterns = [
        r"\bquick\b",
        r"\bbrief\b",
        r"\bsummary\b",
        r"\boverview\b",
        r"\bhigh[- ]level\b",
        r"\bsmoke\b",
    ]
    deep_patterns = [
        r"\bdeep\s+dive\b",
        r"\bdeep\b",
        r"\bthorough\b",
        r"\bcomprehensive\b",
        r"\bend[- ]to[- ]end\b",
        r"\bsecurity\b",
        r"\bvulnerab",
        r"\broot cause\b",
        r"\bproduction\b",
        # Require explicit depth intent; "architecture" alone uses standard
        r"\bdetailed\s+architecture\b",
        r"\barchitectural\s+analysis\b",
        r"\barchitecture\s+review\b",
        r"\bperformance\s+analysis\b",
        r"\bperformance\s+profil",
    ]
    forensic_patterns = [
        r"\bforensic\b",
        r"\bzero[- ]trust\b",
        r"\bincident\b",
        r"\bpost[- ]mortem\b",
        r"\bcompliance\b",
        r"\bcritical vuln",
        r"\bexploit\b",
        r"\bthreat\b",
    ]

    if any(re.search(p, q) for p in forensic_patterns):
        return "forensic"
    if any(re.search(p, q) for p in deep_patterns):
        return "deep"
    if any(re.search(p, q) for p in quick_patterns):
        return "quick"
    return "standard"


def resolve_analysis_depth_profile(query: str, depth_profile: str) -> str:
    """Resolve explicit or auto depth profile (for CLI/API and logging)."""
    normalized = (depth_profile or "auto").strip().lower()
    valid = {"quick", "standard", "deep", "forensic"}
    if normalized in valid:
        return normalized
    return infer_analysis_depth_from_query(query)


class AuthorizationError(Exception):
    """Raised when an authorization check fails."""
    pass

def requires_role(role: str):
    """Decorator to enforce role-based access control before execution."""
    def decorator(func):
        @wraps(func)
        def wrapper(*args, **kwargs):
            # In a real system, this would check a JWT token or session context.
            # Here we provide a foundational environment-based check.
            user_role = os.environ.get("AGENT_USER_ROLE", "ADMIN")
            if user_role != role:
                raise AuthorizationError(f"Access denied: Requires role {role}, got {user_role}")
            return func(*args, **kwargs)
        return wrapper
    return decorator

class AgentManager:
    """
    Orchestrates multi-agent codebase analysis with review cycles.

    The manager coordinates between Code Analyzer and Task Specialist,
    implementing a review cycle where the specialist can provide feedback
    for up to 3 iterations, after which the analysis is forcibly accepted.
    """

    def __init__(self, config_manager: ConfigurationManager):
        """
        Initialize the Agent Manager.

        Args:
            config_manager: Configuration manager for LLM settings
        """
        self.config_manager = config_manager
        self.logger = logging.getLogger(__name__)
        self.structured_logger = get_structured_logger()
        
        # SQLite-backed memory is initialized once codebase path is known.
        self.memory_tool: MemoryTool | None = None
        self.memory_backend = (
            self.config_manager.get_config_value("MEMORY_BACKEND", "sqlite") or "sqlite"
        ).strip().lower()
        if self.memory_backend not in {"sqlite", "none"}:
            self.logger.warning(
                "Unknown MEMORY_BACKEND '%s'; defaulting to sqlite.",
                self.memory_backend,
            )
            self.memory_backend = "sqlite"
        
        self.max_specialist_reviews = self.config_manager.get_max_specialist_reviews()
        self.logger.debug(
            "MAX_SPECIALIST_REVIEWS=%s", self.max_specialist_reviews
        )
        self.skip_graphify_index = self.config_manager.get_skip_graphify_index()
        self.first_review_min_confidence = (
            self.config_manager.get_first_review_min_confidence()
        )

        # Initialize agents
        self.code_analyzer: CodeAnalyzer | None = None
        self.task_specialist: TaskSpecialist | None = None
        self.file_system_tool: FileSystemTool | None = None
        
        # Graphify components
        self.graphify_cli: GraphifyCLI | None = None
        self.graphify_tool: GraphifyTool | None = None
        self.min_quality_confidence = 0.6

    def initialize_agents(self, codebase_path: str = ".") -> None:
        """
        Initialize the multi-agent system foundation.

        Args:
            codebase_path: The target codebase directory. Defaults to ".".
            
        Raises:
            Exception: If agent initialization fails
        """
        try:
            model_client = self.config_manager.get_model_client()

            # Create file system tool with actual codebase path
            file_system_tool = FileSystemTool(codebase_path)
            self.file_system_tool = file_system_tool
            if self.memory_backend == "sqlite":
                self.memory_tool = MemoryTool(
                    str((file_system_tool.working_directory / ".agent_memory.db"))
                )
            else:
                self.memory_tool = None

            # Initialize Graphify with actual codebase path
            self.graphify_cli = GraphifyCLI(codebase_path)
            self.graphify_tool = GraphifyTool(codebase_path)

            self.code_analyzer = CodeAnalyzer(
                model_client,
                file_system_tool,
                self.graphify_tool,
                analysis_depth_profile="standard",
            )
            self.task_specialist = TaskSpecialist(
                model_client,
                max_reviews=self.max_specialist_reviews,
                first_review_min_confidence=self.first_review_min_confidence,
            )

            self.logger.info("Successfully initialized all agents with Graphify support")

        except Exception as e:
            self.logger.error(f"Failed to initialize agents: {e}")
            raise

    @requires_role("ADMIN")
    def process_query_with_review_cycle(
        self,
        query: str,
        codebase_path: str,
        playbook_names: list[str] = None,
        depth_profile: str = "auto",
    ) -> tuple[str, dict]:
        """
        Process user query through multi-playbook pipeline and review cycles.

        Args:
            query: User's task description
            codebase_path: Path to the codebase to analyze
            playbook_names: Optional list of playbook names for sequential chaining.

        Returns:
            Tuple of (final_combined_response, statistics)
        """
        if not self.code_analyzer or not self.task_specialist:
            raise RuntimeError(
                "Agents not initialized. Call initialize_agents() first."
            )

        # Start structured logging session
        agents_involved = ["code_analyzer", "task_specialist"]
        session_id = self.structured_logger.start_session(
            user_query=query, 
            codebase_path=codebase_path, 
            agents_involved=agents_involved
        )
        self.logger.info(f"Started analysis session: {session_id}")
        self.current_session_id = session_id # Track for memory usage

        playbooks_to_run = playbook_names if playbook_names else [None]
        selected_depth_profile = self._resolve_depth_profile(query, depth_profile)

        # Initialize global statistics tracking
        overall_stats = {
            "total_review_cycles": 0,
            "rejections": 0,
            "final_acceptance_type": "unknown",
            "final_confidence": 0.0,
            "completed_playbooks": 0,
            "failed_playbooks": 0,
            "tools_used": {},
            "memory_backend": self.memory_backend,
            "skip_graphify_index": self.skip_graphify_index,
        }

        iter_cap = iteration_cap_for_depth_profile(selected_depth_profile)
        self.logger.info(
            "Analysis plan: depth=%s (≤%s analyzer LLM iterations per review round), "
            "specialist_rounds≤%s, graphify=%s",
            selected_depth_profile,
            iter_cap,
            self.max_specialist_reviews,
            "skipped" if self.skip_graphify_index else "enabled",
        )

        # Pre-scan playbook metadata so per-playbook flags (e.g. skip_graphify_index)
        # can influence steps that run before the playbook loop (e.g. Graphify indexing).
        effective_skip_graphify = self.skip_graphify_index
        if not effective_skip_graphify and playbooks_to_run != [None]:
            pm_pre = PlaybookManager()
            for _pname in playbooks_to_run:
                if not _pname:
                    continue
                _pb = pm_pre.load_playbook(_pname)
                if _pb and _pb.metadata.get("skip_graphify_index"):
                    effective_skip_graphify = True
                    self.logger.info(
                        "Playbook '%s' sets skip_graphify_index=true — skipping Graphify indexing.",
                        _pname,
                    )
                    break

        # Run Graphify indexing before starting review cycles
        initial_findings = []
        if effective_skip_graphify:
            self.logger.info(
                "SKIP_GRAPHIFY_INDEX is set — skipping Graphify indexing (faster startup; "
                "structural graph context omitted unless already on disk)."
            )
        elif self.graphify_cli:
            self.logger.info("Running Graphify indexing...")
            if self.graphify_cli.index():
                self.logger.info("Graphify indexing completed successfully")
                report = self.graphify_cli.read_report()
                if report:
                    self.logger.info(f"Loaded graph report ({len(report)} bytes)")
                    initial_findings.append(f"📊 GRAPH ARCHITECTURE REPORT (Structural Analysis):\n{report[:2000]}...")
                else:
                    self.logger.warning("Graphify indexing succeeded but report file not found")
            else:
                self.logger.warning("Graphify indexing failed - proceeding with standard analysis")
        
        # ADDED: Shallow directory listing for ground-truth path awareness
        try:
            if not self.file_system_tool:
                raise RuntimeError("FileSystemTool not initialized")
            root_contents = self.file_system_tool.list_directory(".")
            initial_findings.append(f"📂 PROJECT STRUCTURE (Root Directory):\n{root_contents}")
        except Exception as e:
            self.logger.warning(f"Failed to get root directory listing: {e}")
        
        all_final_responses = []
        previous_chain_findings = None

        for p_name in playbooks_to_run:
            self.logger.info(f"--- Starting execution for playbook: {p_name or 'DEFAULT'} ---")
            
            # Load instructions and instantiate CodeAnalyzer specific to this playbook
            playbook_instructions = None
            playbook_metadata: dict = {}
            if p_name:
                pm = PlaybookManager()
                playbook = pm.load_playbook(p_name)
                if playbook:
                    self.logger.info(f"Loaded playbook instructions for: {p_name}")
                    playbook_instructions = playbook.sanitize_for_tools(available_tools=["shell", "graphify"])
                    playbook_metadata = playbook.metadata or {}
                else:
                    error_msg = f"Playbook '{p_name}' not found. Marking as error and continuing."
                    self.logger.error(error_msg)
                    all_final_responses.append(f"## Playbook: {p_name}\n**ERROR**: {error_msg}")
                    overall_stats["failed_playbooks"] += 1
                    continue
            
            # Allow playbook frontmatter to override specialist review count.
            # e.g. doc playbooks set max_specialist_reviews: 1 to avoid re-running
            # the full 8-iteration analysis cycle for a review that adds little value.
            effective_max_reviews = self.max_specialist_reviews
            if "max_specialist_reviews" in playbook_metadata:
                try:
                    pb_reviews = int(playbook_metadata["max_specialist_reviews"])
                    if pb_reviews >= 1:
                        effective_max_reviews = pb_reviews
                        self.logger.info(
                            "Playbook overrides max_specialist_reviews: %s → %s",
                            self.max_specialist_reviews, effective_max_reviews,
                        )
                except (TypeError, ValueError):
                    pass
            
            try:
                # Prepare fresh agent instances for this playbook stage
                model_client = self.config_manager.get_model_client()
                from pathlib import Path
                file_system_tool = FileSystemTool(str(Path(codebase_path).resolve()))
                
                self.code_analyzer = CodeAnalyzer(
                    model_client, 
                    file_system_tool, 
                    self.graphify_tool, 
                    playbook_instructions=playbook_instructions,
                    playbook_metadata=playbook_metadata,
                    structured_logger=self.structured_logger,
                    memory_tool=self.memory_tool,
                    session_id=self.current_session_id,
                    analysis_depth_profile=selected_depth_profile,
                )
                
                self.task_specialist = TaskSpecialist(
                    model_client,
                    playbook_instructions=playbook_instructions,
                    structured_logger=self.structured_logger,
                    max_reviews=effective_max_reviews,
                    first_review_min_confidence=self.first_review_min_confidence,
                )
                
                # Incorporate context from previous playbook into initial findings
                current_initial_findings = initial_findings.copy()
                if previous_chain_findings:
                    current_initial_findings.append(
                        f"📊 FINDINGS FROM PREVIOUS PLAYBOOK STAGE:\n{previous_chain_findings}"
                    )
                
                # Execute single cycle loop internally
                response, stats_output = self._run_single_review_cycle(
                    query=query, 
                    codebase_path=codebase_path,
                    initial_findings=current_initial_findings,
                    max_reviews=effective_max_reviews,
                )

                quality_ok, quality_reasons = self._passes_quality_gate(response, stats_output)
                if not quality_ok:
                    overall_stats["failed_playbooks"] += 1
                    playbook_title_display = p_name if p_name else "Default Analysis"
                    all_final_responses.append(
                        f"## Playbook: {playbook_title_display}\n"
                        "**ERROR (Quality Gate Failed)**:\n"
                        + "\n".join(f"- {r}" for r in quality_reasons)
                    )
                    continue
                
                # Update stats
                overall_stats["total_review_cycles"] += stats_output["total_review_cycles"]
                overall_stats["rejections"] += stats_output["rejections"]
                overall_stats["final_acceptance_type"] = stats_output["final_acceptance_type"]
                overall_stats["final_confidence"] = stats_output["final_confidence"]
                overall_stats["completed_playbooks"] += 1
                overall_stats["analysis_depth_profile"] = selected_depth_profile
                
                # Accumulate tool usage stats
                for tool, count in file_system_tool.usage_stats.items():
                    overall_stats["tools_used"][tool] = overall_stats["tools_used"].get(tool, 0) + count
                
                playbook_title_display = p_name if p_name else "Default Analysis"
                formatted_resp = f"## Results for: {playbook_title_display}\n\n{self._sanitize_user_output(response)}"
                all_final_responses.append(formatted_resp)
                
                # The output context becomes available for the next playbook
                previous_chain_findings = response

                # Only inject graphify context once at start, so clear for subsequent runs
                initial_findings = []
                
            except Exception as e:
                self.logger.error(f"Execution of playbook '{p_name}' failed: {e}")
                overall_stats["failed_playbooks"] += 1
                all_final_responses.append(f"## Playbook: {p_name}\n**ERROR (Execution Failed)**: {e}")
                continue

        # Add graphify stats at the end since it's shared across playbooks
        if self.graphify_tool and hasattr(self.graphify_tool, 'usage_stats'):
            for tool, count in self.graphify_tool.usage_stats.items():
                overall_stats["tools_used"][tool] = overall_stats["tools_used"].get(tool, 0) + count

        final_combined_response = "\n\n---\n\n".join(all_final_responses)
        
        # End structured logging session
        self.structured_logger.end_session(final_combined_response)
        overall_stats["session_id"] = session_id
        
        # Save final result to persistent memory
        if self.memory_tool:
            self.memory_tool.add_memory(
                f"FINAL ANALYSIS RESULT for '{query}':\n\n{final_combined_response}",
                user_id="global_knowledge",
            )
        
        return final_combined_response, overall_stats

    def _resolve_depth_profile(self, query: str, depth_profile: str) -> str:
        """
        Resolve final depth profile.
        - If depth_profile is explicit (quick|standard|deep|forensic), use it.
        - If auto/unknown, infer from user query intent.
        """
        return resolve_analysis_depth_profile(query, depth_profile)

    def _run_single_review_cycle(
        self, query: str, codebase_path: str, initial_findings: list[str], max_reviews: int
    ) -> tuple[str, dict]:
        """Runs the loop of specialist reviewing the code analyzer for a single context."""
        statistics = {
            "total_review_cycles": 0,
            "rejections": 0,
            "final_acceptance_type": "unknown",
            "final_confidence": 0.0,
            "analyzer_metrics": {},
        }

        specialist_feedback = None
        review_count = 0
        
        # Base code analyzer uses its internal loop reference - assuming it's correctly patched to self
        while review_count < max_reviews:
            review_count += 1
            statistics["total_review_cycles"] = review_count

            self.logger.info(f"Starting review cycle {review_count}/{max_reviews}")

            # Code Analyzer analyzes the codebase
            self.logger.info("Code Analyzer starting analysis...")
            analysis_result = self.code_analyzer.analyze_codebase(
                query, codebase_path, specialist_feedback, initial_findings
            )
            statistics["analyzer_metrics"] = getattr(self.code_analyzer, "last_run_metrics", {}) or {}
            
            # Log completion of analyzer iteration
            self.structured_logger.log_iteration_complete(
                agent="code_analyzer",
                iteration_number=review_count,
                total_commands=0, # Need to track this in analyzer
                self_assessment=analysis_result[:200], # Snippet for assessment
                continue_analysis=True
            )
            
            # Clear initial findings after first cycle so they don't keep appending
            initial_findings = []

            # Task Specialist reviews the analysis
            self.logger.info("Task Specialist reviewing analysis...")
            self.structured_logger.log_review_start(
                agent="task_specialist",
                review_number=review_count,
                report_length=len(analysis_result),
                review_criteria=["technical_depth", "evidence_based", "actionability"]
            )
            (
                is_complete,
                feedback_message,
                confidence_score,
            ) = self.task_specialist.review_analysis(
                analysis_result, query, review_count
            )
            
            # Log review completion
            self.structured_logger.log_review_complete(
                agent="task_specialist",
                review_number=review_count,
                is_complete=is_complete,
                missing_areas=[feedback_message[:100]],
                feedback_provided=feedback_message
            )

            # Check if specialist accepts the analysis
            if is_complete:
                statistics["final_acceptance_type"] = "accepted"
                statistics["final_confidence"] = confidence_score
                self.logger.info(f"Analysis accepted on review cycle {review_count} with confidence {confidence_score:.2f}")
                return self._synthesize_final_response(analysis_result, True, feedback_message, query), statistics

            statistics["rejections"] += 1

            if review_count >= max_reviews:
                statistics["final_acceptance_type"] = "forced"
                statistics["final_confidence"] = confidence_score
                self.logger.warning(f"Max reviews ({max_reviews}) reached. Force accepting analysis.")
                return self._synthesize_final_response(analysis_result, False, feedback_message, query), statistics

            self.logger.info(f"Analysis rejected. Feedback: {feedback_message}")
            specialist_feedback = feedback_message

        statistics["final_acceptance_type"] = "forced"
        statistics["final_confidence"] = 0.0
        return self._synthesize_final_response(analysis_result, False, "Completed via fallback.", query), statistics

    def _sanitize_user_output(self, text: str) -> str:
        """Strip internal tool/protocol artifacts from user-visible output."""
        if not text:
            return text
        cleaned_lines = []
        for ln in text.splitlines():
            if re.search(r"<\|channel\|>|to=\w+|repo_browser\.", ln, re.IGNORECASE):
                continue
            cleaned_lines.append(ln)
        return "\n".join(cleaned_lines).strip()

    def _passes_quality_gate(self, response: str, stats_output: dict) -> tuple[bool, list[str]]:
        """Fail fast for null-work or leaked-internals outputs."""
        reasons: list[str] = []
        metrics = stats_output.get("analyzer_metrics", {}) if isinstance(stats_output, dict) else {}

        if metrics.get("total_actions", 0) == 0:
            reasons.append("No analysis operations executed (0 actions).")
        if metrics.get("touched_file_count", 0) < 1:
            reasons.append("Insufficient code coverage (no files touched).")
        if metrics.get("status") == "insufficient_evidence":
            reasons.append("Analyzer reported insufficient evidence.")
        if float(stats_output.get("final_confidence", 0.0) or 0.0) < self.min_quality_confidence:
            reasons.append(
                f"Low final confidence ({stats_output.get('final_confidence', 0.0):.2f} < {self.min_quality_confidence:.2f})."
            )
        if re.search(r"<\|channel\|>|to=\w+|repo_browser\.", response or "", re.IGNORECASE):
            reasons.append("Internal tool/protocol artifacts detected in final response.")
        if "EVIDENCE CHECK WARNING" in (response or ""):
            reasons.append("Evidence citation checks failed.")
        missing = metrics.get("missing_required_actions", [])
        if missing:
            missing_str = ", ".join(f"`{a}`" for a in missing)
            reasons.append(
                f"Playbook-required actions never executed: {missing_str}. "
                "The agent completed analysis without producing mandatory output artifacts."
            )
        wrong_paths = metrics.get("wrong_path_writes", [])
        if wrong_paths:
            prefix = metrics.get("required_output_path_prefix", "")
            reasons.append(
                f"write_file outputs are outside the required path `{prefix}`: {wrong_paths}. "
                "Wiki files must be written inside the correct directory."
            )
        if not metrics.get("write_count_ok", True):
            n = metrics.get("min_write_file_count", 0)
            written = metrics.get("written_paths", [])
            reasons.append(
                f"Insufficient wiki files written: {len(written)} written, "
                f"minimum required is {n}."
            )
        return len(reasons) == 0, reasons


    def _synthesize_final_response(
        self,
        analysis_result: str,
        is_accepted: bool,
        feedback_message: str,
        original_query: str,
    ) -> str:
        """
        Synthesize the final response from analysis and review results.

        Args:
            analysis_result: The final analysis from Code Analyzer
            is_accepted: Whether the analysis was accepted by the specialist
            feedback_message: The feedback from Task Specialist
            original_query: The original user query

        Returns:
            Synthesized final response
        """
        # Create a comprehensive response that includes both the analysis and any final insights
        final_response = f"""# Codebase Analysis Results

## Task: {original_query}

## Analysis:
{analysis_result}
"""

        # Add specialist insights if available and positive
        if is_accepted and feedback_message:
            final_response += f"""

## Specialist Review:
{feedback_message}
"""

        # Add any warnings or notes for forced acceptance
        if not is_accepted:
            final_response += """

## Note:
This analysis was completed after reaching the maximum number of review cycles. While comprehensive, there may be areas that could benefit from further investigation.
"""
            if feedback_message:
                final_response += f"""

## Areas for Further Investigation:
{feedback_message}
"""

        return final_response

    def get_agent(self, agent_name: str) -> Any:
        """
        Retrieve specific agent by name.

        Args:
            agent_name: Name of the agent ('code_analyzer' or 'task_specialist')

        Returns:
            The requested agent instance

        Raises:
            ValueError: If agent name is invalid
            RuntimeError: If agents not initialized
        """
        if not self.code_analyzer or not self.task_specialist:
            raise RuntimeError(
                "Agents not initialized. Call initialize_agents() first."
            )

        if agent_name == "code_analyzer":
            return self.code_analyzer
        elif agent_name == "task_specialist":
            return self.task_specialist
        else:
            raise ValueError(
                f"Unknown agent name: {agent_name}. Available: 'code_analyzer', 'task_specialist'"
            )
