# agents.py

import requests
import json
import re
import time
from memory import memory
from post_processor import post_processor

POP_API_URL = "https://api-stage-agents.popai.agency/api/1/chat/chat-stream"
AUTH_TOKEN = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJBcHBsaWNhdGlvbklkIjoiNGY1YTZiN2M4ZDllMGYxYTJiM2M0ZDVlNmY3ZzhoOWkiLCJBcHBsaWNhdGlvbk5hbWUiOiJDaGF0Ym9zcyIsIm5iZiI6MTc2OTY4Mjc0OSwiZXhwIjo0OTI1MzU2MzQ5LCJpYXQiOjE3Njk2ODI3NDl9.Ck4yqyEH3J_RfwUGk7UPjuXpXMEMzDmetEKG03ykrOE"
CONFIGURATION_ID = "cfb4cf83bfa94eb98e2c0bee0dd49d6c"
WORKSPACE_ID = "853e49f2afbb4ba8a62c7084d7327172"
TIMEOUT = 120

# Per-session state (company name + last JD for edits)
SESSION_DATA = {}


def _get_session(session_id):
    if session_id not in SESSION_DATA:
        SESSION_DATA[session_id] = {
            "company_name": None,
            "last_job_title": None,
        }
    return SESSION_DATA[session_id]


def _replace_invented_company(text: str) -> str:
    """Replace PopAI's invented company names with [Company Name]."""
    invented_name = None

    match = re.search(r'\*\*About\s+([^\*\n]+?)\*\*', text, re.IGNORECASE)
    if match:
        invented_name = match.group(1).strip()

    if not invented_name:
        match = re.search(
            r'(?:^|\n)([A-Z][A-Za-z0-9& ,.]{1,50}?)\s+is\s+a\b',
            text, re.MULTILINE
        )
        if match:
            candidate = match.group(1).strip().rstrip(',').strip()
            skip = {"The", "This", "We", "Our", "A", "An", "Position"}
            if candidate not in skip and len(candidate) > 1:
                invented_name = candidate

    if invented_name:
        return text.replace(invented_name, "[Company Name]")
    return text


class ExternalAgent:

    def stream_execution(self, session_id, prompt):

        memory.add_message(session_id, "user", prompt)
        session = _get_session(session_id)

        # -----------------------------------------
        # 1️⃣ Understand intent + extract fields (single LLM call)
        # -----------------------------------------

        history = memory.get_history(session_id)

        understood = post_processor.understand_user_intent(
            user_message=prompt,
            conversation_history=history,
            current_job_title=session.get("last_job_title"),
        )

        intent = understood.get("intent", "provide_info")
        extracted_title = understood.get("job_title")
        print(f"[DEBUG] Intent: {intent}, Title: {extracted_title}")

        # -----------------------------------------
        # 2️⃣ Handle company name
        # -----------------------------------------

        # Guard: discard hallucinated company names
        if understood.get("company_name"):
            if understood["company_name"].lower() not in prompt.lower():
                print(f"[DEBUG] Discarding hallucinated company: '{understood['company_name']}'")
                understood["company_name"] = None

        if understood.get("company_name"):
            session["company_name"] = understood["company_name"]

        # Also catch explicit "change company to X" requests
        change_match = re.search(
            r'(?:change|set|update|use)\s+(?:the\s+)?company\s+(?:name\s+)?(?:to|as)\s+(.+)',
            prompt, re.IGNORECASE
        )
        if change_match:
            session["company_name"] = change_match.group(1).strip().rstrip('.')

        current_company = session["company_name"]

        # -----------------------------------------
        # 3️⃣ Route based on intent
        # -----------------------------------------

        if intent == "change_role" and extracted_title:
            # ---- User wants a DIFFERENT role ----
            old_title = session.get("last_job_title")
            print(f"[DEBUG] Changing role: '{old_title}' → '{extracted_title}'")

            # Reset accumulated fields for fresh generation
            session.pop("accumulated_fields", None)
            session.pop("pending", None)
            session["last_job_title"] = extracted_title

            # Merge any fields the user provided alongside the new title
            session["accumulated_fields"] = {}
            for field in ["skills", "department", "experience_level", "tasks"]:
                val = understood.get(field)
                if val:
                    session["accumulated_fields"][field] = val

            # Proceed to generate (falls through to new JD generation below)
            intent = "create_new"

        if intent == "edit_jd":
            # ---- User wants to edit the existing JD ----
            previous_jd = ""
            for msg in reversed(history):
                if msg["role"] == "assistant" and len(msg["content"]) > 200:
                    previous_jd = msg["content"]
                    break

            if previous_jd:
                company_instruction = (
                    f"Use the company name '{current_company}' throughout."
                    if current_company
                    else "Use '[Company Name]' as a placeholder. Do NOT invent a company name."
                )

                final_prompt = f"""
Here is an existing job description:
---
{previous_jd}
---

The user wants the following change: {prompt}

{company_instruction}

Please update the job description accordingly. Keep everything else unchanged.
"""
            else:
                # No previous JD found — treat as new creation
                intent = "create_new"

        if intent == "auto_fill":
            # ---- User wants the system to fill in missing fields ----
            print("[DEBUG] Auto-fill intent detected — skipping questions")
            # Treat as create_new but skip all questions
            intent = "create_new"

        if intent in ("create_new", "provide_info"):
            # ---- New JD creation or user providing more info ----

            # Determine job title
            job_title = extracted_title or session.get("last_job_title", "")

            if not job_title:
                follow_up = (
                    "I'd love to help generate a job description! "
                    "Could you tell me the **job title** you're looking for? "
                    "For example: 'Junior Laravel Developer', 'Senior Data Engineer', etc."
                )
                memory.add_message(session_id, "assistant", follow_up)
                yield {"type": "content", "value": follow_up}
                session["pending"] = {"extracted": understood}
                return

            # Track the job title in session
            session["last_job_title"] = job_title

            # --------------------------------------------------
            # Accumulate fields across multiple turns
            # --------------------------------------------------
            if "accumulated_fields" not in session:
                session["accumulated_fields"] = {}
            acc = session["accumulated_fields"]

            # Merge fields from current extraction
            for field in ["skills", "department", "experience_level", "tasks"]:
                val = understood.get(field)
                if val:
                    acc[field] = val

            # Also merge from any pending state
            if session.get("pending"):
                pending = session.pop("pending")
                prev_extracted = pending.get("extracted", {})
                for field in ["skills", "department", "experience_level", "tasks"]:
                    if not acc.get(field) and prev_extracted.get(field):
                        acc[field] = prev_extracted[field]

            skills = acc.get("skills")

            # Only ask for skills if the user hasn't requested auto-fill
            if not skills and understood.get("intent") != "auto_fill":
                follow_up = (
                    f"Great! I'll create a JD for **{job_title}**.\n\n"
                    "What **skills** should this role require? "
                    "For example: 'Laravel, PHP, Angular', 'Python, AWS, Docker', etc.\n\n"
                    "Or say **'fill those for me'** to let me decide."
                )
                memory.add_message(session_id, "assistant", follow_up)
                yield {"type": "content", "value": follow_up}
                session["pending"] = {
                    "extracted": {**understood, "job_title": job_title, **acc}
                }
                return

            # Use accumulated fields, auto-fill the rest
            department = acc.get("department")
            tasks = acc.get("tasks")
            experience_level = acc.get("experience_level")

            fields = {
                "department": department,
                "tasks": tasks,
                "skills": skills,
            }

            auto_fill_needed = [k for k, v in fields.items() if not v]

            if auto_fill_needed:
                print(f"[DEBUG] Auto-filling: {auto_fill_needed}")
                auto_filled = post_processor.auto_fill_missing_fields(
                    job_title=job_title,
                    experience_level=experience_level,
                    existing_fields={
                        "department": fields["department"],
                        "tasks": fields["tasks"],
                        "skills": skills,
                    }
                )
                for field in auto_fill_needed:
                    if field in auto_filled and auto_filled[field]:
                        fields[field] = auto_filled[field]

            # Pull skills back out (may have been auto-filled)
            skills = fields.get("skills") or skills

            company_line = (
                f"Company: {current_company}"
                if current_company
                else "Company: [Company Name] (use exactly this placeholder, "
                     "do NOT invent a company name)"
            )

            print(f"[DEBUG] Final state — title: {job_title}, "
                  f"dept: {fields.get('department')}, level: {experience_level}, "
                  f"skills: {skills}, tasks: {fields.get('tasks')}")

            final_prompt = f"""
You are a professional HR content writer. Generate a complete, polished job description using ONLY the details below. Do NOT ask any follow-up questions. Do NOT request clarification. Output the full job description immediately.

{company_line}
Job Title: {job_title}
Department: {fields.get('department', 'General')}
Experience Level: {experience_level or 'Not specified'}
Tasks/Responsibilities: {fields.get('tasks', 'General duties')}
Skills: {skills}

IMPORTANT: Generate the complete job description NOW. Include all standard sections (About, Responsibilities, Qualifications, etc). Do NOT ask for more information.
"""

        print(f"[DEBUG] Final prompt preview: {final_prompt[:300]}")

        # -----------------------------------------
        # 4️⃣ Call PopAI
        # -----------------------------------------

        headers = {"Authorization": f"Bearer {AUTH_TOKEN}"}
        data = {
            "configurationId": CONFIGURATION_ID,
            "userPrompt": final_prompt,
            "workspaceId": WORKSPACE_ID
        }

        run_id = None
        full_content = ""
        session_state = None

        with requests.post(
            POP_API_URL,
            headers=headers,
            data=data,
            stream=True,
            timeout=TIMEOUT
        ) as response:

            print(f"[DEBUG] PopAI status: {response.status_code}")

            if response.status_code != 200:
                raise Exception(
                    f"PopAI Error: {response.status_code} {response.text}"
                )

            for line in response.iter_lines():
                if not line:
                    continue

                decoded = line.decode("utf-8")
                if not decoded.startswith("data: "):
                    continue

                payload = decoded[6:]
                try:
                    event_data = json.loads(payload)
                except Exception:
                    continue

                event_type = event_data.get("event")

                if event_data.get("run_id"):
                    run_id = event_data.get("run_id")
                if event_data.get("session_state"):
                    session_state = event_data.get("session_state")

                if event_type == "RunContent":
                    content = event_data.get("content")
                    if content:
                        full_content += content

                if event_type == "RunCompleted":
                    completed_content = event_data.get("content", "")

                    if completed_content:
                        jd_text = None
                        if isinstance(completed_content, dict):
                            jd_text = completed_content.get(
                                "full_description", ""
                            )
                        elif isinstance(completed_content, str):
                            try:
                                parsed = json.loads(completed_content)
                                jd_text = parsed.get("full_description", "")
                            except (json.JSONDecodeError, AttributeError):
                                jd_text = None

                        if jd_text:
                            full_content = jd_text
                        elif not full_content:
                            full_content = str(completed_content)

                    break

        print(f"[DEBUG] full_content length: {len(full_content)}")

        # -----------------------------------------
        # 5️⃣ Save execution metadata
        # -----------------------------------------

        memory.save_execution(
            session_id=session_id,
            run_id=run_id,
            session_state=session_state,
            final_output=full_content
        )

        # -----------------------------------------
        # 6️⃣ Handle company name in output
        # -----------------------------------------

        if full_content.strip():
            if current_company:
                full_content = _replace_invented_company(full_content)
                full_content = full_content.replace(
                    "[Company Name]", current_company
                )
                full_content = full_content.replace(
                    "[company name]", current_company
                )
                full_content = full_content.replace(
                    "[COMPANY NAME]", current_company
                )
            else:
                full_content = _replace_invented_company(full_content)

        # -----------------------------------------
        # 7️⃣ Clean up & stream
        # -----------------------------------------

        if full_content.strip():
            output = re.sub(
                r'\n*Generated on:.*$', '', full_content,
                flags=re.IGNORECASE | re.DOTALL
            )
            output = re.sub(
                r'\n*Date:?\s*\d{4}[-/]\d{2}[-/]\d{2}.*$', '', output,
                flags=re.DOTALL
            )
            output = output.rstrip()

            if output.strip():
                memory.add_message(session_id, "assistant", output)

                chunk_size = 20
                for i in range(0, len(output), chunk_size):
                    yield {
                        "type": "content",
                        "value": output[i:i + chunk_size],
                    }
                    time.sleep(0.03)
            else:
                yield {
                    "type": "content",
                    "value": "⚠️ The response was empty. Please try again.",
                }
        else:
            yield {
                "type": "content",
                "value": "⚠️ No response generated. Please try again "
                         "with more details.",
            }


agent = ExternalAgent()


def route(prompt: str):
    return agent