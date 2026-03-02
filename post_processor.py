# post_processor.py

import os
import re
import json
from typing import Dict, Any, List
from huggingface_hub import InferenceClient

HF_MODEL = "meta-llama/Meta-Llama-3-8B-Instruct"
HF_TOKEN = os.getenv("HF_TOKEN")  # safer than hardcoding


class HFPostProcessor:

    def __init__(self):
        if not HF_TOKEN:
            raise ValueError("HF_TOKEN environment variable not set.")

        self.client = InferenceClient(
            model=HF_MODEL,
            token=HF_TOKEN
        )

    # ---------------------------------------------------
    # Helper: Clean JSON from LLM responses
    # ---------------------------------------------------

    @staticmethod
    def _clean_json_response(raw: str) -> dict:
        """
        LLMs (especially Llama) often wrap JSON in markdown code blocks:
          ```json\n{...}\n```
        This method strips those wrappers before parsing.
        """
        text = raw.strip()

        # Strip ```json ... ``` or ``` ... ``` wrappers
        md_match = re.search(r'```(?:json)?\s*\n?(.*?)\n?\s*```', text, re.DOTALL)
        if md_match:
            text = md_match.group(1).strip()

        # Sometimes the model prefixes with text before the JSON object
        # Try to find the first { ... } block
        if not text.startswith('{'):
            brace_match = re.search(r'\{.*\}', text, re.DOTALL)
            if brace_match:
                text = brace_match.group(0)

        return json.loads(text)

    # ---------------------------------------------------
    # 1️⃣ Understand User Intent + Extract Fields (single LLM call)
    # ---------------------------------------------------

    def understand_user_intent(
        self,
        user_message: str,
        conversation_history: List[Dict] = None,
        current_job_title: str = None,
    ) -> Dict[str, Any]:
        """
        Single smart LLM call that handles BOTH:
        - Intent classification (what does the user want to do?)
        - Field extraction (what JD data is in their message?)

        Returns:
        {
            "intent": "create_new" | "change_role" | "edit_jd" | "provide_info",
            "job_title": str | null,
            "company_name": str | null,
            "department": str | null,
            "experience_level": str | null,
            "tasks": str | null,
            "skills": str | null
        }
        """

        # Build a concise conversation summary for context
        conv_context = ""
        if conversation_history:
            # Only include last 6 messages for context
            recent = conversation_history[-6:]
            conv_lines = []
            for msg in recent:
                role = "User" if msg["role"] == "user" else "Assistant"
                content = msg["content"]
                if len(content) > 150:
                    content = content[:150] + "..."
                conv_lines.append(f"{role}: {content}")
            conv_context = "\n".join(conv_lines)

        current_title_line = ""
        if current_job_title:
            current_title_line = f'\nThe currently active job title being discussed is: "{current_job_title}"'

        system_prompt = (
            "You are an intelligent assistant that analyzes user messages in a "
            "job description generator chatbot.\n\n"
            "Your job is to determine:\n"
            "1. What the user INTENDS to do\n"
            "2. What job description FIELDS are present in their message\n\n"
            "INTENT RULES:\n"
            '- "create_new": User wants to create a brand new job description '
            '(e.g. "create a JD for software engineer", "I need a data analyst '
            'job description", or they just type a job title like "Junior Laravel Developer")\n'
            '- "create_job_ad": User wants to create a JOB ADVERTISEMENT or JOB AD '
            '(not a job description). Look for keywords like "job ad", "job advertisement", '
            '"job posting", "post a job", "create an ad", "hiring ad". '
            'e.g. "create a job ad for senior ML engineer", "I want to post a job ad", '
            '"generate a job advertisement for data analyst"\n'
            '- "change_role": User wants to switch to a DIFFERENT job role/title '
            'than the current one (e.g. "now I want a junior react developer", '
            '"make it AI Specialist instead", "switch to data engineer"). '
            "This is when they mention a NEW job title different from the current one.\n"
            '- "edit_jd": User wants to modify an EXISTING job description without '
            'changing the role (e.g. "add Python to the skills", "change the department '
            'to Engineering", "make it more senior")\n'
            '- "provide_info": User is answering a question from the assistant or '
            "providing additional information like skills, department, experience level, "
            'or tasks (e.g. "Python, React, Node.js", "AI Department, Mid-level")\n'
            '- "auto_fill": User wants the system to automatically fill in missing fields '
            "instead of providing them. This includes phrases like "
            '"fill those for me", "you decide", "just generate it", "skip", '
            '"auto fill", "use defaults", "I don\'t know", "whatever you think", '
            '"can you fill those up for me", "fill those all required field for me", '
            '"just pick something", "I\'ll leave it to you"\n'
            f"{current_title_line}\n\n"
            "FIELD EXTRACTION RULES:\n"
            "- Extract ONLY information that is EXPLICITLY stated or clearly implied\n"
            "- job_title: The job role/position mentioned. Extract it even from natural "
            'language (e.g. "I want a junior react developer" -> "Junior React Developer")\n'
            "- company_name: ONLY if the user explicitly names a company. Never invent one.\n"
            "- department, experience_level, tasks, skills: Extract if mentioned\n"
            "- If a field is not mentioned, return null\n\n"
            "Return ONLY valid JSON with these exact keys: intent, job_title, "
            "company_name, department, experience_level, tasks, skills\n"
            "No markdown. No backticks. No explanations. ONLY the JSON object.\n\n"
            "EXAMPLES:\n\n"
            'User: "create a job description for senior backend developer"\n'
            '{"intent": "create_new", "job_title": "Senior Backend Developer", '
            '"company_name": null, "department": null, "experience_level": "senior", '
            '"tasks": null, "skills": null}\n\n'
            'User: "now I want a junior react developer" (current title: "AI Specialist")\n'
            '{"intent": "change_role", "job_title": "Junior React Developer", '
            '"company_name": null, "department": null, "experience_level": "junior", '
            '"tasks": null, "skills": null}\n\n'
            'User: "make it AI Specialist instead" (current title: "Developer")\n'
            '{"intent": "change_role", "job_title": "AI Specialist", "company_name": null, '
            '"department": null, "experience_level": null, "tasks": null, "skills": null}\n\n'
            'User: "Python, React, Node.js"\n'
            '{"intent": "provide_info", "job_title": null, "company_name": null, '
            '"department": null, "experience_level": null, "tasks": null, '
            '"skills": ["Python", "React", "Node.js"]}\n\n'
            'User: "AI Department, Mid-level, create complex workflows"\n'
            '{"intent": "provide_info", "job_title": null, "company_name": null, '
            '"department": "AI Department", "experience_level": "Mid-level", '
            '"tasks": "create complex workflows", "skills": null}\n\n'
            'User: "add Docker to the skills"\n'
            '{"intent": "edit_jd", "job_title": null, "company_name": null, '
            '"department": null, "experience_level": null, "tasks": null, '
            '"skills": ["Docker"]}\n\n'
            'User: "Junior Laravel Developer"\n'
            '{"intent": "create_new", "job_title": "Junior Laravel Developer", '
            '"company_name": null, "department": null, "experience_level": "junior", '
            '"tasks": null, "skills": null}\n\n'
            'User: "fill those for me"\n'
            '{"intent": "auto_fill", "job_title": null, "company_name": null, '
            '"department": null, "experience_level": null, "tasks": null, "skills": null}\n\n'
            'User: "just generate it"\n'
            '{"intent": "auto_fill", "job_title": null, "company_name": null, '
            '"department": null, "experience_level": null, "tasks": null, "skills": null}\n\n'
            'User: "you decide"\n'
            '{"intent": "auto_fill", "job_title": null, "company_name": null, '
            '"department": null, "experience_level": null, "tasks": null, "skills": null}\n\n'
            'User: "create a job ad for senior ML engineer"\n'
            '{"intent": "create_job_ad", "job_title": "Senior ML Engineer", '
            '"company_name": null, "department": null, "experience_level": "senior", '
            '"tasks": null, "skills": null}\n\n'
            'User: "I want to post a job advertisement for data analyst"\n'
            '{"intent": "create_job_ad", "job_title": "Data Analyst", '
            '"company_name": null, "department": null, "experience_level": null, '
            '"tasks": null, "skills": null}\n\n'
            'User: "generate a hiring ad for junior laravel developer"\n'
            '{"intent": "create_job_ad", "job_title": "Junior Laravel Developer", '
            '"company_name": null, "department": null, "experience_level": "junior", '
            '"tasks": null, "skills": null}'
        )

        # Build the user message with conversation context
        if conv_context:
            user_prompt = (
                f"Conversation so far:\n{conv_context}\n\n"
                f'Latest user message: "{user_message}"'
            )
        else:
            user_prompt = f'User message: "{user_message}"'

        response = self.client.chat_completion(
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            max_tokens=300,
            temperature=0,
        )

        raw_content = response.choices[0].message["content"]
        print(f"[DEBUG] HF understand_intent raw: {raw_content[:500]}")

        try:
            result = self._clean_json_response(raw_content)
            valid_intents = {"create_new", "create_job_ad", "change_role", "edit_jd", "provide_info", "auto_fill"}
            if result.get("intent") not in valid_intents:
                result["intent"] = "provide_info"
            print(f"[DEBUG] HF understood: intent={result.get('intent')}, "
                  f"title={result.get('job_title')}, skills={result.get('skills')}")
            return result
        except (json.JSONDecodeError, Exception) as e:
            print(f"[DEBUG] HF intent parse failed: {e} | raw: {raw_content[:300]}")
            return {
                "intent": "provide_info",
                "job_title": None,
                "company_name": None,
                "department": None,
                "experience_level": None,
                "tasks": None,
                "skills": None,
            }

    # ---------------------------------------------------
    # 2️⃣ Format Job Description (Post-generation polish)
    # ---------------------------------------------------

    def format_job_description(self, raw_text: str) -> str:

        system_prompt = (
            "You are a formatting assistant.\n"
            "You MUST NOT invent new responsibilities, skills, or details.\n"
            "You MUST NOT remove information.\n"
            "You MUST ONLY reformat and improve readability.\n"
            "If something is missing, do not guess.\n"
            "Preserve all original content exactly.\n"
        )

        user_prompt = f"""
Reformat the following job description into a clean professional structure:

- Add clear section headers
- Use bullet points where appropriate
- Improve spacing
- Keep exact meaning

Job Description:
{raw_text}
"""

        response = self.client.chat_completion(
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            max_tokens=800,
            temperature=0.2,
        )

        return response.choices[0].message["content"]

    # ---------------------------------------------------
    # 3️⃣ Smart Auto-Fill Missing Fields
    # ---------------------------------------------------

    def auto_fill_missing_fields(
        self,
        job_title: str,
        experience_level: str = None,
        existing_fields: Dict = None,
    ) -> Dict[str, Any]:
        """
        Given a job title (and optional experience level),
        generate reasonable defaults for any missing fields.
        """

        existing_info = ""
        if existing_fields:
            for key, value in existing_fields.items():
                if value:
                    existing_info += f"\n{key}: {value}"

        system_prompt = """
You are an HR expert assistant.

Given a job title and any available context, generate reasonable professional defaults for missing job description fields.

Return ONLY valid JSON with these fields:
- department: the most likely department for this role
- tasks: a comma-separated list of 4-5 typical responsibilities
- skills: a comma-separated list of 4-5 key skills required
- experience_level: e.g. "Entry-level", "2+ years", "5+ years experience"
- benefits: a comma-separated list of 3-4 typical benefits (e.g. "HMO, 15 days PTO, flexible hours, annual bonus")
- salary_range: a realistic salary range for this role (e.g. "PHP 25,000 - 35,000 per month")

Be specific and realistic based on the job title.
Do NOT return fields that are already provided.

No markdown. No backticks. No explanations. ONLY valid JSON.
"""

        user_prompt = f"""
Job Title: {job_title}
Experience Level: {experience_level or "Not specified"}
Already known:{existing_info if existing_info else " None"}

Generate reasonable defaults for any missing fields (department, tasks, skills, experience_level, benefits, salary_range).
"""

        response = self.client.chat_completion(
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            max_tokens=500,
            temperature=0.3,
        )

        raw_content = response.choices[0].message["content"]
        print(f"[DEBUG] HF auto_fill raw: {raw_content[:500]}")

        try:
            result = self._clean_json_response(raw_content)
            print(f"[DEBUG] HF auto_fill parsed: {result}")
            return result
        except (json.JSONDecodeError, Exception) as e:
            print(f"[DEBUG] HF auto_fill JSON parse failed: {e}")
            return {
                "department": "General",
                "tasks": "Collaborating with team members, Contributing to projects, "
                         "Maintaining documentation, Supporting daily operations",
                "skills": "Communication, Problem-solving, Time management, "
                          "Technical aptitude",
                "experience_level": "Not specified",
                "benefits": "HMO, PTO, flexible hours",
                "salary_range": "Competitive",
            }


# Singleton instance
post_processor = HFPostProcessor()