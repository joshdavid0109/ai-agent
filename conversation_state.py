# conversation_state.py

from typing import Optional, List, Dict


class JDConversationState:

    def __init__(self):
        self.job_title: Optional[str] = None
        self.company_name: Optional[str] = None
        self.department: Optional[str] = None
        self.experience_level: Optional[str] = None
        self.tasks: Optional[str] = None
        self.skills: Optional[str] = None

    def update(self, extracted: Dict):
        """
        Update state with extracted fields.
        Only overwrite if value is not None.
        """
        for key, value in extracted.items():
            if value:
                setattr(self, key, value)

    def has_critical_fields(self) -> bool:
        """
        Smart Hybrid: Only the job_title is truly critical.
        Everything else can be auto-generated from context.
        """
        return bool(self.job_title)

    def is_complete(self) -> bool:
        """
        Check if ALL fields are filled (either by user or auto-generated).
        """
        return all([
            self.job_title,
            self.department,
            self.tasks,
            self.skills
        ])

    def missing_fields(self) -> List[str]:
        missing = []

        if not self.job_title:
            missing.append("job_title")
        if not self.department:
            missing.append("department")
        if not self.tasks:
            missing.append("tasks")
        if not self.skills:
            missing.append("skills")

        return missing

    def auto_filled_fields(self) -> Dict[str, str]:
        """
        Returns a dict of fields that were auto-filled (for transparency).
        Tracked via the _auto_filled set.
        """
        return {field: getattr(self, field) for field in getattr(self, '_auto_filled', set())}

    def mark_auto_filled(self, fields: List[str]):
        """Track which fields were auto-generated."""
        if not hasattr(self, '_auto_filled'):
            self._auto_filled = set()
        self._auto_filled.update(fields)