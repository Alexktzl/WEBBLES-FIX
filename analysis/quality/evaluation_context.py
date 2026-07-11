"""
Контекст оценки качества патча для webles_conveyor.
"""

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


@dataclass
class EvaluationContext:
    before_errors: List[Dict[str, Any]] = field(default_factory=list)
    after_errors: List[Dict[str, Any]] = field(default_factory=list)
    validation_results: Dict[str, Any] = field(default_factory=dict)
    before_validation: Optional[Dict[str, Any]] = None
    patch_size: int = 0
    files_modified: int = 0
    iteration_count: int = 0
    search_depth: int = 0
    beam_scores: Optional[List[float]] = None
    intent: Optional[str] = None
    patch_source: str = ""