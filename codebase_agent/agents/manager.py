"""
Agent Manager for orchestrating multi-agent codebase analysis.

This module implements the central orchestration layer that coordinates
Code Analyzer and Task Specialist through a review cycle mechanism.
"""

import logging
import os
from typing import Any, Optional
from functools import wraps

from ..config.configuration import ConfigurationManager
from ..tools.file_system_tool import FileSystemTool
from .code_analyzer import CodeAnalyzer
from .task_specialist import TaskSpecialist
from ..utils.playbook import PlaybookManager, Playbook
from ..utils.graphify_cli import GraphifyCLI
from ..tools.graphify_tool import GraphifyTool

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
        self.max_specialist_reviews = 20

        # Initialize agents
        self.code_analyzer: CodeAnalyzer | None = None
        self.task_specialist: TaskSpecialist | None = None
        
        # Graphify components
        self.graphify_cli: GraphifyCLI | None = None
        self.graphify_tool: GraphifyTool | None = None

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

            # Initialize Graphify with actual codebase path
            self.graphify_cli = GraphifyCLI(codebase_path)
            self.graphify_tool = GraphifyTool(codebase_path)

            self.code_analyzer = CodeAnalyzer(model_client, file_system_tool, self.graphify_tool)
            self.task_specialist = TaskSpecialist(model_client)

            self.logger.info("Successfully initialized all agents with Graphify support")

        except Exception as e:
            self.logger.error(f"Failed to initialize agents: {e}")
            raise

    @requires_role("ADMIN")
    def process_query_with_review_cycle(
        self, query: str, codebase_path: str, playbook_names: list[str] = None
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

        self.logger.info(f"Starting analysis for query: {query}")
        self.logger.info(f"Codebase path: {codebase_path}")

        playbooks_to_run = playbook_names if playbook_names else [None]

        # Initialize global statistics tracking
        overall_stats = {
            "total_review_cycles": 0,
            "rejections": 0,
            "final_acceptance_type": "unknown",
            "final_confidence": 0.0,
            "completed_playbooks": 0,
            "failed_playbooks": 0,
            "tools_used": {}
        }

        # Run Graphify indexing before starting review cycles
        self.logger.info("Running Graphify indexing...")
        initial_findings = []
        if self.graphify_cli:
            
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
        
        all_final_responses = []
        previous_chain_findings = None

        for p_name in playbooks_to_run:
            self.logger.info(f"--- Starting execution for playbook: {p_name or 'DEFAULT'} ---")
            
            # Load instructions and instantiate CodeAnalyzer specific to this playbook
            playbook_instructions = None
            if p_name:
                pm = PlaybookManager()
                playbook = pm.load_playbook(p_name)
                if playbook:
                    self.logger.info(f"Loaded playbook instructions for: {p_name}")
                    playbook_instructions = playbook.sanitize_for_tools(available_tools=["shell", "graphify"])
                else:
                    error_msg = f"Playbook '{p_name}' not found. Marking as error and continuing."
                    self.logger.error(error_msg)
                    all_final_responses.append(f"## Playbook: {p_name}\n**ERROR**: {error_msg}")
                    overall_stats["failed_playbooks"] += 1
                    continue
            
            try:
                # Prepare a fresh CodeAnalyzer instance for this iteration 
                model_client = self.config_manager.get_model_client()
                from pathlib import Path
                file_system_tool = FileSystemTool(str(Path(codebase_path).resolve()))
                self.code_analyzer = CodeAnalyzer(
                    model_client, file_system_tool, self.graphify_tool, playbook_instructions=playbook_instructions
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
                    initial_findings=current_initial_findings
                )
                
                # Update stats
                overall_stats["total_review_cycles"] += stats_output["total_review_cycles"]
                overall_stats["rejections"] += stats_output["rejections"]
                overall_stats["final_acceptance_type"] = stats_output["final_acceptance_type"]
                overall_stats["final_confidence"] = stats_output["final_confidence"]
                overall_stats["completed_playbooks"] += 1
                
                # Accumulate tool usage stats
                for tool, count in file_system_tool.usage_stats.items():
                    overall_stats["tools_used"][tool] = overall_stats["tools_used"].get(tool, 0) + count
                
                playbook_title_display = p_name if p_name else "Default Analysis"
                formatted_resp = f"## Results for: {playbook_title_display}\n\n{response}"
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
        return final_combined_response, overall_stats

    def _run_single_review_cycle(
        self, query: str, codebase_path: str, initial_findings: list[str]
    ) -> tuple[str, dict]:
        """Runs the loop of specialist reviewing the code analyzer for a single context."""
        statistics = {
            "total_review_cycles": 0,
            "rejections": 0,
            "final_acceptance_type": "unknown",
            "final_confidence": 0.0,
        }

        specialist_feedback = None
        review_count = 0
        
        # Base code analyzer uses its internal loop reference - assuming it's correctly patched to self
        while review_count < self.max_specialist_reviews:
            review_count += 1
            statistics["total_review_cycles"] = review_count

            self.logger.info(f"Starting review cycle {review_count}/{self.max_specialist_reviews}")

            # Code Analyzer analyzes the codebase
            self.logger.info("Code Analyzer starting analysis...")
            analysis_result = self.code_analyzer.analyze_codebase(
                query, codebase_path, specialist_feedback, initial_findings
            )
            
            # Clear initial findings after first cycle so they don't keep appending
            initial_findings = []

            # Task Specialist reviews the analysis
            self.logger.info("Task Specialist reviewing analysis...")
            (
                is_complete,
                feedback_message,
                confidence_score,
            ) = self.task_specialist.review_analysis(
                analysis_result, query, review_count
            )

            # Check if specialist accepts the analysis
            if is_complete:
                statistics["final_acceptance_type"] = "accepted"
                statistics["final_confidence"] = confidence_score
                self.logger.info(f"Analysis accepted on review cycle {review_count} with confidence {confidence_score:.2f}")
                return self._synthesize_final_response(analysis_result, True, feedback_message, query), statistics

            statistics["rejections"] += 1

            if review_count >= self.max_specialist_reviews:
                statistics["final_acceptance_type"] = "forced"
                statistics["final_confidence"] = confidence_score
                self.logger.warning(f"Max reviews ({self.max_specialist_reviews}) reached. Force accepting analysis.")
                return self._synthesize_final_response(analysis_result, False, feedback_message, query), statistics

            self.logger.info(f"Analysis rejected. Feedback: {feedback_message}")
            specialist_feedback = feedback_message

        statistics["final_acceptance_type"] = "forced"
        statistics["final_confidence"] = 0.0
        return self._synthesize_final_response(analysis_result, False, "Completed via fallback.", query), statistics


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
