import logging
import time
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, HTTPException, status
from pydantic import BaseModel, Field, validator
import os

from ..agents.manager import AgentManager, AuthorizationError
from ..config.configuration import ConfigurationError, ConfigurationManager
from ..utils.logging import setup_logging

logger = logging.getLogger(__name__)

app = FastAPI(
    title="Codebase Agent API",
    description="REST API for the AutoGen-powered Codebase Understanding Agent",
    version="1.0.0",
)


class AnalyzeRequest(BaseModel):
    codebase_path: str = Field(..., description="Path to the codebase to analyze")
    task_description: str = Field(..., max_length=2000, description="Task or query description")
    playbooks: Optional[str] = Field(None, description="Comma-separated playbooks")

    @validator('codebase_path')
    def validate_codebase_path(cls, v):
        """Prevent path traversal and basic sanitization."""
        # Normalize path
        normalized = os.path.normpath(v)
        # Check for obvious traversal tokens
        if ".." in normalized.split(os.sep):
            raise ValueError("Path traversal ('..') is not permitted in codebase_path.")
        return normalized


class AnalyzeResponse(BaseModel):
    codebase_path: str
    task_description: str
    analysis_result: str
    execution_time: float
    timestamp: str
    statistics: dict


@app.on_event("startup")
def startup_event():
    # Setup basic logging for the API
    setup_logging("INFO", "logs", "INFO")
    logger.info("FastAPI Server started for Codebase Agent")


@app.get("/health", tags=["System"])
def health_check():
    """Simple health check endpoint."""
    return {"status": "ok", "message": "Codebase Agent API is running"}


@app.post("/analyze", response_model=AnalyzeResponse, tags=["Analysis"])
def analyze_codebase(request: AnalyzeRequest):
    """
    Analyze codebase for a specific development task.
    This endpoint runs synchronously on a separate thread by FastAPI,
    allowing multiple concurrent requests to be processed without blocking the event loop.
    """
    start_time = time.time()
    codebase_path_obj = Path(request.codebase_path).resolve()

    if not codebase_path_obj.exists() or not codebase_path_obj.is_dir():
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Codebase path does not exist or is not a directory: {request.codebase_path}",
        )

    try:
        logger.info(f"Starting analysis for codebase: {codebase_path_obj}")
        
        # Initialize configuration
        config_manager = ConfigurationManager(codebase_path_obj)
        config_manager.load_environment()

        missing_keys = config_manager.validate_configuration()
        if missing_keys:
            error_msg = f"Missing configuration keys in .env: {', '.join(missing_keys)}"
            logger.error(error_msg)
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=error_msg,
            )

        # Initialize agent manager
        agent_manager = AgentManager(config_manager)
        agent_manager.initialize_agents(str(codebase_path_obj))

        # Parse playbooks list
        playbooks_list = [p.strip() for p in request.playbooks.split(",")] if request.playbooks else []

        # Execute the analysis
        # Using the codebase path as the working directory as well
        result, statistics = agent_manager.process_query_with_review_cycle(
            request.task_description, str(codebase_path_obj), playbook_names=playbooks_list
        )

        execution_time = time.time() - start_time
        logger.info(f"Analysis completed successfully in {execution_time:.2f} seconds")

        return AnalyzeResponse(
            codebase_path=str(codebase_path_obj),
            task_description=request.task_description,
            analysis_result=result,
            execution_time=execution_time,
            timestamp=time.strftime("%Y-%m-%d %H:%M:%S"),
            statistics=statistics,
        )

    except ConfigurationError as e:
        logger.error(f"Configuration error: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Configuration Error: {e}",
        )
    except AuthorizationError as e:
        logger.error(f"Authorization error: {e}")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=str(e),
        )
    except PermissionError as e:
        logger.error(f"Permission error: {e}")
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"Permission denied: {e}",
        )
    except Exception as e:
        logger.exception("Unexpected error during analysis")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"An unexpected error occurred during analysis: {e}",
        )
