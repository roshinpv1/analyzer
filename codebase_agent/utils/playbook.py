import os
import re
import yaml
import logging
from pathlib import Path
from typing import Dict, Any, List, Optional

logger = logging.getLogger(__name__)

class Playbook:
    """Represents a structured agentic playbook."""
    
    def __init__(self, name: str, content: str):
        self.name = name
        self.raw_content = content
        self.metadata: Dict[str, Any] = {}
        self.sections: Dict[str, str] = {}
        self._parse()

    def _parse(self):
        """Parse markdown content and extract YAML frontmatter and sections."""
        # Extract YAML frontmatter
        frontmatter_match = re.match(r'^---\s*\n(.*?)\n---\s*\n', self.raw_content, re.DOTALL)
        if frontmatter_match:
            try:
                self.metadata = yaml.safe_load(frontmatter_match.group(1))
                content_after_frontmatter = self.raw_content[frontmatter_match.end():]
            except Exception as e:
                logger.error(f"Failed to parse YAML frontmatter in playbook {self.name}: {e}")
                content_after_frontmatter = self.raw_content
        else:
            content_after_frontmatter = self.raw_content

        # Extract sections based on ## headers (case-insensitive)
        current_section = "General"
        sections = {current_section: []}
        
        lines = content_after_frontmatter.split('\n')
        for line in lines:
            header_match = re.match(r'^##\s+(.*)', line)
            if header_match:
                current_section = header_match.group(1).strip()
                sections[current_section] = []
            else:
                if current_section in sections:
                    sections[current_section].append(line)
        
        self.sections = {name: '\n'.join(lines).strip() for name, lines in sections.items() if lines}
        
    def get_section(self, section_name: str) -> Optional[str]:
        """Get a section by name (case-insensitive)."""
        lower_name = section_name.lower()
        for k, v in self.sections.items():
            if k.lower() == lower_name:
                return v
        return None

    def get_system_instructions(self) -> str:
        """Combine relevant sections for inclusion in an agent's system prompt."""
        instructions = []
        
        # Use case-insensitive lookup
        sys_prompt = self.get_section("System Prompt")
        if sys_prompt:
            instructions.append(f"### PLAYBOOK STRATEGY: {self.name.upper()}\n{sys_prompt}")
        
        procedure = self.get_section("Procedure")
        if procedure:
            instructions.append(f"### OPERATIONAL PROCEDURE\n{procedure}")
            
        anti_patterns = self.get_section("Anti-Patterns")
        if anti_patterns:
            instructions.append(f"### CRITICAL: WHAT NOT TO DO\n{anti_patterns}")
            
        output_schema = self.get_section("Output Schema")
        if output_schema:
            instructions.append(f"### TARGET OUTPUT STRUCTURE\n{output_schema}")

        return "\n\n".join(instructions)

    def sanitize_for_tools(self, available_tools: List[str]):
        """
        'Correct' the playbook by mapping external tool references to available ones
        and warning about unavailable capabilities.
        """
        # Mapping of external/Legacy tool names to current ones
        tool_mapping = {
            "get_map": "graphify_tool",
            "search_codebase": "grep_search / ls",
            "search_symbol": "grep_search",
            "trace_path": "graphify_tool",
            "get_dependencies": "graphify_tool",
            "CatalogEntryWriter": "INTERNAL_RESULT_PROCESSOR",
            "RETRIEVED CODE": "SEARCH RESULTS"
        }
        
        instructions = self.get_system_instructions()
        
        # Apply mappings
        for old, new in tool_mapping.items():
            instructions = instructions.replace(old, new)
            
        return instructions


class PlaybookManager:
    """Manages discovery and loading of playbooks."""
    
    def __init__(self, playbooks_dir: Optional[str] = None):
        if playbooks_dir:
            self.playbooks_dir = Path(playbooks_dir)
        else:
            # Default to repo_root/codebase_agent/playbooks
            try:
                import codebase_agent
                self.playbooks_dir = Path(codebase_agent.__file__).parent / "playbooks"
            except ImportError:
                self.playbooks_dir = Path("codebase_agent/playbooks")
                
        if not self.playbooks_dir.exists():
            logger.warning(f"Playbooks directory not found: {self.playbooks_dir}")

    def list_playbooks(self) -> List[Dict[str, Any]]:
        """List all available playbooks and their metadata."""
        if not self.playbooks_dir.exists():
            return []
            
        playbooks = []
        for file in self.playbooks_dir.glob("*.md"):
            try:
                pb = self.load_playbook(file.stem)
                if pb:
                    playbooks.append({
                        "name": file.stem,
                        "description": pb.metadata.get("description", "No description available"),
                        "category": pb.metadata.get("category", "General"),
                        "complexity": pb.metadata.get("complexity", "Unknown")
                    })
            except Exception as e:
                logger.error(f"Failed to list playbook {file.name}: {e}")
                
        return playbooks

    def load_playbook(self, name: str) -> Optional[Playbook]:
        """Load a specific playbook by name."""
        file_path = self.playbooks_dir / f"{name}.md"
        if not file_path.exists():
            logger.error(f"Playbook not found: {file_path}")
            return None
            
        try:
            with open(file_path, 'r', encoding='utf-8') as f:
                content = f.read()
            return Playbook(name, content)
        except Exception as e:
            logger.error(f"Failed to load playbook {name}: {e}")
            return None
