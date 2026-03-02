# agents.py

import requests
import json
import re
import time
from memory import memory
from post_processor import post_processor

POP_API_URL = "https://api-stage-agents.popai.agency/api/1/chat/chat-stream"
AUTH_TOKEN = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJBcHBsaWNhdGlvbklkIjoiNGY1YTZiN2M4ZDllMGYxYTJiM2M0ZDVlNmY3ZzhoOWkiLCJBcHBsaWNhdGlvbk5hbWUiOiJDaGF0Ym9zcyIsIm5iZiI6MTc2OTY4Mjc0OSwiZXhwIjo0OTI1MzU2MzQ5LCJpYXQiOjE3Njk2ODI3NDl9.Ck4yqyEH3J_RfwUGk7UPjuXpXMEMzDmetEKG03ykrOE"
CONFIGURATION_ID = "cfb4cf83bfa94eb98e2c0bee0dd49d6c"  # JD generator
JOB_AD_CONFIGURATION_ID = "3760263646d04386b41e901ff4fa44ba"  # Job Ad Creator
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

    # ---------------------------------------------------------
    # Job Ad Creator — dedicated PopAI agent for job ads
    # ---------------------------------------------------------
    def _call_job_ad_creator(self, session_id, session, prompt):
        """
        Calls the PopAI Job Ad Creator API with a comprehensive prompt.
        Parses JSON responses (GATHER_INFO follow-ups vs final DRAFT_ADS).
        """

        print(f"[DEBUG] Job Ad API prompt: {prompt[:500]}")

        headers = {"Authorization": f"Bearer {AUTH_TOKEN}"}
        form_data = {
            "configurationId": JOB_AD_CONFIGURATION_ID,
            "userPrompt": prompt,
            "workspaceId": WORKSPACE_ID,
        }

        # Also pass sessionId if available
        if session.get("job_ad_session_id"):
            form_data["sessionId"] = session["job_ad_session_id"]

        run_id = None
        full_content = ""
        session_state = None
        job_ad_session_id = None

        with requests.post(
            POP_API_URL,
            headers=headers,
            data=form_data,
            stream=True,
            timeout=TIMEOUT,
        ) as response:
            print(f"[DEBUG] Job Ad Creator status: {response.status_code}")

            if response.status_code != 200:
                raise Exception(
                    f"PopAI Job Ad Error: {response.status_code} {response.text}"
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
                if event_data.get("session_id"):
                    job_ad_session_id = event_data.get("session_id")
                if event_data.get("session_state"):
                    session_state = event_data.get("session_state")

                if event_type == "RunContent":
                    content = event_data.get("content")
                    if content:
                        full_content += content

                if event_type == "RunCompleted":
                    completed_content = event_data.get("content", "")
                    if completed_content and not full_content:
                        full_content = str(completed_content)
                    if event_data.get("session_id"):
                        job_ad_session_id = event_data.get("session_id")
                    break

        # Store the Job Ad Creator's session ID for next turn
        if job_ad_session_id:
            session["job_ad_session_id"] = job_ad_session_id
            print(f"[DEBUG] Stored Job Ad sessionId: {job_ad_session_id}")

        print(f"[DEBUG] Job Ad raw content length: {len(full_content)}")
        print(f"[DEBUG] Job Ad raw content preview: {full_content[:500]}")

        # -----------------------------------------
        # Parse the JSON response from the Job Ad Creator
        # -----------------------------------------
        display_text = ""
        action = None

        if full_content.strip():
            try:
                cleaned = full_content.strip()

                # Strip markdown ```json ... ``` wrappers (multiple patterns)
                # Pattern 1: ```json ... ```
                md_match = re.search(
                    r'```(?:json)?\s*\n?(.*?)\n?\s*```',
                    cleaned, re.DOTALL
                )
                if md_match:
                    cleaned = md_match.group(1).strip()

                # Pattern 2: If starts with ```json or ```
                if cleaned.startswith('```'):
                    cleaned = re.sub(r'^```(?:json)?\s*\n?', '', cleaned)
                    cleaned = re.sub(r'\n?\s*```\s*$', '', cleaned)
                    cleaned = cleaned.strip()

                # Try parsing
                try:
                    parsed = json.loads(cleaned)
                except json.JSONDecodeError:
                    # Fallback: try to extract JSON object from the content
                    json_match = re.search(r'\{.*\}', cleaned, re.DOTALL)
                    if json_match:
                        parsed = json.loads(json_match.group(0))
                    else:
                        raise

                action = parsed.get("action")
                response_text = parsed.get("response", "")

                if action == "GATHER_INFO" and response_text:
                    # The agent is asking follow-up questions
                    display_text = response_text

                elif action == "DRAFT_ADS" and parsed.get("platforms"):
                    # Final ad generation with platform-specific ads
                    parts = []
                    # API uses camelCase "jobTitle"
                    job_title = parsed.get("jobTitle") or parsed.get("job_title", "")
                    if job_title:
                        parts.append(f"## 📋 Job Ad: {job_title}\n")

                    platforms = parsed.get("platforms", {})
                    for platform_name, platform_data in platforms.items():
                        try:
                            if len(platforms) > 1:
                                parts.append(f"### 📢 {platform_name.title()}\n")

                            if isinstance(platform_data, str):
                                parts.append(platform_data)
                                parts.append("")
                                continue

                            if not isinstance(platform_data, dict):
                                parts.append(str(platform_data))
                                parts.append("")
                                continue

                            desc = platform_data.get("description", "")
                            if desc:
                                # Convert escaped newlines to actual newlines
                                desc = desc.replace("\\n", "\n")
                                parts.append(desc)

                            link = platform_data.get("downloadable_file_link")
                            if link:
                                parts.append(f"\n📥 [Download Ad]({link})")

                            # Show structured fields if present
                            structured = platform_data.get("structuredFields") or platform_data.get("structured_fields") or {}
                            if structured:
                                parts.append("\n**Details:**")
                                for key, val in structured.items():
                                    if isinstance(val, list):
                                        val = ", ".join(str(v) for v in val)
                                    parts.append(f"- **{key}:** {val}")

                            parts.append("")
                        except Exception as e:
                            print(f"[DEBUG] Error processing platform '{platform_name}': {e}")
                            parts.append(str(platform_data))
                            parts.append("")

                    if parsed.get("platform_count"):
                        parts.append(
                            f"\n*Generated for {parsed['platform_count']} platform(s)*"
                        )

                    display_text = "\n".join(parts)

                elif parsed.get("platforms"):
                    # Fallback for other action types with platforms
                    parts = []
                    job_title = parsed.get("jobTitle") or parsed.get("job_title", "")
                    if job_title:
                        parts.append(f"## 📋 Job Ad: {job_title}\n")
                    platforms = parsed.get("platforms", {})
                    for platform_name, platform_data in platforms.items():
                        if len(platforms) > 1:
                            parts.append(f"### 📢 {platform_name.title()}\n")
                        desc = platform_data.get("description", "")
                        if desc:
                            desc = desc.replace("\\n", "\n")
                            parts.append(desc)
                        parts.append("")
                    display_text = "\n".join(parts)

                elif response_text:
                    display_text = response_text
                else:
                    display_text = full_content

            except (json.JSONDecodeError, Exception) as e:
                print(f"[DEBUG] Job Ad JSON parse failed: {e}")
                display_text = full_content

        # Save execution metadata
        memory.save_execution(
            session_id=session_id,
            run_id=run_id,
            session_state=session_state,
            final_output=display_text,
        )

        # If the agent completed the ad (not GATHER_INFO), exit job_ad mode
        # but remember that the last output was a job ad for edit routing
        if action and action != "GATHER_INFO":
            print(f"[DEBUG] Job Ad completed (action={action}) — exiting job_ad mode")
            session.pop("mode", None)
            session.pop("job_ad_session_id", None)
            session["last_output_type"] = "job_ad"

        # Stream output to user
        if display_text.strip():
            memory.add_message(session_id, "assistant", display_text)
            chunk_size = 20
            for i in range(0, len(display_text), chunk_size):
                yield {"type": "content", "value": display_text[i:i + chunk_size]}
                time.sleep(0.03)
        else:
            yield {
                "type": "content",
                "value": "⚠️ No response from the Job Ad Creator. Please try again.",
            }

    # ---------------------------------------------------------
    # Build comprehensive prompt & call Job Ad Creator
    # ---------------------------------------------------------
    def _build_and_call_job_ad(self, session_id, session):
        """
        Uses HF to auto-fill ALL missing details, then builds a comprehensive
        prompt matching the format that produces the best, most detailed ads.
        """
        ad_fields = session.get("job_ad_fields", {})

        job_title = ad_fields.get("job_title", "")
        platforms = ad_fields.get("platforms", "Facebook")
        skills = ad_fields.get("skills")
        tasks = ad_fields.get("tasks")
        department = ad_fields.get("department")
        experience_level = ad_fields.get("experience_level")
        location = ad_fields.get("location")
        salary = ad_fields.get("salary")
        benefits = ad_fields.get("benefits")
        company_name = ad_fields.get("company_name") or session.get("company_name")

        # Auto-fill ALL missing fields using HF
        if job_title:
            needs_fill = (
                not skills or not tasks or not department
                or not experience_level or not benefits or not salary
            )
            if needs_fill:
                fill_list = []
                if not skills: fill_list.append("skills")
                if not tasks: fill_list.append("tasks")
                if not department: fill_list.append("department")
                if not experience_level: fill_list.append("experience_level")
                if not benefits: fill_list.append("benefits")
                if not salary: fill_list.append("salary_range")
                print(f"[DEBUG] Job Ad — auto-filling: {fill_list}")

                auto_filled = post_processor.auto_fill_missing_fields(
                    job_title=job_title,
                    experience_level=experience_level,
                    existing_fields={
                        "skills": skills,
                        "tasks": tasks,
                        "department": department,
                        "benefits": benefits,
                        "salary": salary,
                    },
                )
                if not skills:
                    skills = auto_filled.get("skills", "Relevant technical skills")
                if not tasks:
                    tasks = auto_filled.get("tasks", "Core role responsibilities")
                if not department:
                    department = auto_filled.get("department", "General")
                if not experience_level:
                    experience_level = auto_filled.get("experience_level", "")
                if not benefits:
                    benefits = auto_filled.get("benefits", "HMO, PTO, flexible hours")
                if not salary:
                    salary = auto_filled.get("salary_range", "Competitive")

        # Default location if not provided
        if not location:
            location = "Remote"

        # Build the comprehensive prompt — ALWAYS include all fields
        # This format consistently produces detailed, professional ads
        # Determine seniority from experience level or job title
        seniority = ad_fields.get("seniority_level", "")
        if not seniority:
            title_lower = (job_title or "").lower()
            if any(w in title_lower for w in ["senior", "lead", "principal", "staff"]):
                seniority = "Senior"
            elif any(w in title_lower for w in ["junior", "entry", "intern", "trainee"]):
                seniority = "Entry-level"
            elif any(w in title_lower for w in ["mid", "intermediate"]):
                seniority = "Mid-Senior level"
            else:
                seniority = "Mid-Senior level"

        employment_type = ad_fields.get("employment_type", "Full-time")
        industry = ad_fields.get("industry", "Information Technology")

        comprehensive_prompt = (
            f"Create a detailed job advertisement for a {job_title or 'the role'} position. "
            f"Post to {platforms}. "
            f"Responsibilities: {tasks or 'Core duties for this role'}. "
            f"Required Skills: {skills or 'Relevant skills'}"
            f"{', ' + experience_level if experience_level else ''}. "
            f"Location: {location}. "
            f"Salary: {salary}. "
            f"Benefits: {benefits}. "
            f"Seniority Level: {seniority}. "
            f"Industry: {industry}. "
            f"Employment Type: {employment_type}."
        )
        if company_name:
            comprehensive_prompt += f" Company: {company_name}."

        print(f"[DEBUG] Job Ad comprehensive prompt: {comprehensive_prompt[:500]}")

        yield from self._call_job_ad_creator(session_id, session, comprehensive_prompt)

    # ---------------------------------------------------------
    # Edit an existing job ad via Job Ad Creator
    # ---------------------------------------------------------
    def _build_and_call_job_ad_with_edit(self, session_id, session, previous_ad, edit_request):
        """
        Regenerates a job ad with edits applied.
        Includes the previous ad content and the user's edit request.
        """
        ad_fields = session.get("job_ad_fields", {})
        platforms = ad_fields.get("platforms", "Facebook")

        edit_prompt = (
            f"Here is an existing job advertisement:\n"
            f"---\n{previous_ad}\n---\n\n"
            f"The user wants the following change: {edit_request}\n\n"
            f"Please regenerate the job ad for {platforms} with this change applied. "
            f"Keep everything else the same. Output the updated ad."
        )

        print(f"[DEBUG] Job Ad edit prompt: {edit_prompt[:500]}")
        yield from self._call_job_ad_creator(session_id, session, edit_prompt)

    def stream_execution(self, session_id, prompt):

        memory.add_message(session_id, "user", prompt)
        session = _get_session(session_id)

        # -----------------------------------------
        # 0️⃣ If already in job_ad mode, check for exit keywords first
        # -----------------------------------------
        if session.get("mode") == "job_ad":
            # Check if the user wants to switch away from job ad mode
            prompt_lower = prompt.lower()
            exit_keywords = [
                "job description", "create a jd", "generate a jd",
                "nevermind", "never mind", "cancel", "forget it",
                "switch to jd", "switch to job description",
                "i want a jd", "create jd",
            ]
            wants_exit = any(kw in prompt_lower for kw in exit_keywords)

            if wants_exit:
                print(f"[DEBUG] User wants to exit job_ad mode — switching to JD flow")
                session.pop("mode", None)
                session.pop("job_ad_session_id", None)
                session.pop("job_ad_fields", None)
                # Fall through to the normal HF intent classification below
            else:
                # -------------------------------------------------
                # Follow-up in job_ad mode — user is providing more info
                # -------------------------------------------------
                print(f"[DEBUG] Job Ad follow-up — processing user response")

                # Parse platform(s) from the user's follow-up if we need them
                ad_fields = session.get("job_ad_fields", {})

                if not ad_fields.get("platforms"):
                    # Extract platforms from the user's response
                    platform_keywords = {
                        "facebook": ["facebook", "fb"],
                        "linkedin": ["linkedin"],
                        "indeed": ["indeed"],
                    }
                    detected_platforms = []
                    for pname, keywords in platform_keywords.items():
                        if any(kw in prompt.lower() for kw in keywords):
                            detected_platforms.append(pname)
                    if "all" in prompt.lower():
                        detected_platforms = ["facebook", "linkedin", "indeed"]

                    if detected_platforms:
                        ad_fields["platforms"] = ", ".join(detected_platforms)
                    else:
                        # Treat the whole response as platform info
                        ad_fields["platforms"] = prompt.strip()

                    session["job_ad_fields"] = ad_fields
                else:
                    # User is providing additional info (location, salary, etc.)
                    # Re-extract fields from the accumulated user messages
                    history = memory.get_history(session_id)
                    all_user_text = " ".join(
                        msg["content"] for msg in history if msg["role"] == "user"
                    )
                    # Try to extract additional fields
                    additional = post_processor.understand_user_intent(
                        user_message=all_user_text,
                        conversation_history=[],
                        current_job_title=ad_fields.get("job_title"),
                    )
                    for field in ["skills", "department", "experience_level", "tasks"]:
                        val = additional.get(field)
                        if val and not ad_fields.get(field):
                            ad_fields[field] = val
                    session["job_ad_fields"] = ad_fields

                # Now build the comprehensive prompt and call the API
                yield from self._build_and_call_job_ad(session_id, session)
                return

        # -----------------------------------------
        # 0.5️⃣ If last output was a job ad, check if user wants to continue with it
        # -----------------------------------------
        if session.get("last_output_type") == "job_ad" and session.get("mode") != "job_ad":
            prompt_lower = prompt.lower()

            # Check if user explicitly wants a JD — only then switch
            jd_keywords = [
                "job description", "create a jd", "generate a jd",
                "switch to jd", "i want a jd", "create jd",
            ]
            wants_jd = any(kw in prompt_lower for kw in jd_keywords)

            if not wants_jd:
                # Check if user mentions a platform → add it and regenerate
                platform_keywords = {
                    "facebook": ["facebook", "fb"],
                    "linkedin": ["linkedin"],
                    "indeed": ["indeed"],
                }
                detected_platforms = []
                for pname, keywords in platform_keywords.items():
                    if any(kw in prompt_lower for kw in keywords):
                        detected_platforms.append(pname)
                if "all" in prompt_lower and "platform" in prompt_lower:
                    detected_platforms = ["facebook", "linkedin", "indeed"]

                # Check for edit-like keywords
                edit_keywords = [
                    "update", "change", "modify", "edit", "add",
                    "remove", "replace", "also", "include",
                ]
                wants_edit = any(kw in prompt_lower for kw in edit_keywords)

                if detected_platforms or wants_edit:
                    print(f"[DEBUG] Staying in job ad context — re-entering job_ad mode")
                    session["mode"] = "job_ad"
                    ad_fields = session.get("job_ad_fields", {})

                    if detected_platforms:
                        # User wants additional platform(s)
                        existing = ad_fields.get("platforms", "")
                        existing_list = [p.strip() for p in existing.split(",") if p.strip()]
                        for p in detected_platforms:
                            if p not in existing_list:
                                existing_list.append(p)
                        ad_fields["platforms"] = ", ".join(existing_list)
                        session["job_ad_fields"] = ad_fields
                        print(f"[DEBUG] Updated platforms: {ad_fields['platforms']}")

                    if wants_edit and not detected_platforms:
                        # User wants to edit the ad
                        previous_ad = ""
                        history = memory.get_history(session_id)
                        for msg in reversed(history):
                            if msg["role"] == "assistant" and len(msg["content"]) > 100:
                                previous_ad = msg["content"]
                                break

                        # Extract any updated fields
                        edit_understood = post_processor.understand_user_intent(
                            user_message=prompt,
                            conversation_history=memory.get_history(session_id),
                            current_job_title=ad_fields.get("job_title"),
                        )
                        for field in ["skills", "department", "experience_level", "tasks"]:
                            val = edit_understood.get(field)
                            if val:
                                ad_fields[field] = val
                        session["job_ad_fields"] = ad_fields

                        yield from self._build_and_call_job_ad_with_edit(
                            session_id, session, previous_ad, prompt
                        )
                        return

                    # Regenerate with updated platforms
                    yield from self._build_and_call_job_ad(session_id, session)
                    return

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

        # Guard: discard platform names mistakenly extracted as company names
        if understood.get("company_name"):
            platform_names = {"facebook", "linkedin", "indeed", "fb", "meta"}
            extracted_lower = understood["company_name"].lower().strip()
            # Check if the "company name" is just platform names
            # e.g. "Facebook", "Facebook and LinkedIn", "LinkedIn, Indeed"
            cleaned = re.sub(r'\b(and|,|&)\b', ' ', extracted_lower)
            words = [w.strip() for w in cleaned.split() if w.strip()]
            if all(w in platform_names for w in words):
                print(f"[DEBUG] Discarding platform as company: '{understood['company_name']}'")
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
            # ---- Check if we should edit a job AD or job DESCRIPTION ----

            if session.get("last_output_type") == "job_ad":
                # User wants to edit the previously generated job ad
                print(f"[DEBUG] Editing job ad — regenerating with updated info")

                ad_fields = session.get("job_ad_fields", {})

                # Apply the user's edit to the ad fields
                # Extract any updated fields from the prompt
                edit_understood = post_processor.understand_user_intent(
                    user_message=prompt,
                    conversation_history=history,
                    current_job_title=ad_fields.get("job_title"),
                )
                for field in ["skills", "department", "experience_level", "tasks"]:
                    val = edit_understood.get(field)
                    if val:
                        ad_fields[field] = val

                # Also parse location/salary from the edit prompt
                prompt_lower = prompt.lower()
                loc_match = re.search(
                    r'(?:location|based in|remote|hybrid)',
                    prompt, re.IGNORECASE
                )
                if loc_match:
                    ad_fields["location"] = prompt.strip()

                salary_match = re.search(
                    r'(?:salary|pay|compensation)[:\s]*([^.]+)',
                    prompt, re.IGNORECASE
                )
                if salary_match:
                    ad_fields["salary"] = salary_match.group(1).strip()

                session["job_ad_fields"] = ad_fields
                session["mode"] = "job_ad"

                # Include the edit request as additional context
                # Get the previous ad content
                previous_ad = ""
                for msg in reversed(history):
                    if msg["role"] == "assistant" and len(msg["content"]) > 100:
                        previous_ad = msg["content"]
                        break

                # Build updated prompt with the edit context
                ad_fields_copy = dict(ad_fields)
                yield from self._build_and_call_job_ad_with_edit(
                    session_id, session, previous_ad, prompt
                )
                return

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
                session["last_output_type"] = "job_description"
            else:
                # No previous JD found — treat as new creation
                intent = "create_new"

        if intent == "auto_fill":
            # ---- User wants the system to fill in missing fields ----
            print("[DEBUG] Auto-fill intent detected — skipping questions")
            # Treat as create_new but skip all questions
            intent = "create_new"

        # -----------------------------------------
        # Job Ad creation (initial trigger via HF intent)
        # -----------------------------------------
        if intent == "create_job_ad":
            session["mode"] = "job_ad"
            print(f"[DEBUG] New job_ad mode — extracting fields from initial prompt")

            # Extract fields from the user's initial message
            job_title = extracted_title or ""
            ad_fields = {
                "job_title": job_title,
                "skills": understood.get("skills"),
                "department": understood.get("department"),
                "experience_level": understood.get("experience_level"),
                "tasks": understood.get("tasks"),
                "company_name": current_company,
            }

            # Detect platforms from the initial prompt
            platform_keywords = {
                "facebook": ["facebook", "fb"],
                "linkedin": ["linkedin"],
                "indeed": ["indeed"],
            }
            detected_platforms = []
            prompt_lower = prompt.lower()
            for pname, keywords in platform_keywords.items():
                if any(kw in prompt_lower for kw in keywords):
                    detected_platforms.append(pname)
            if "all" in prompt_lower and (
                "platform" in prompt_lower or "all" in prompt_lower
            ):
                detected_platforms = ["facebook", "linkedin", "indeed"]

            if detected_platforms:
                ad_fields["platforms"] = ", ".join(detected_platforms)

            # Detect location from prompt
            location_match = re.search(
                r'(?:location|based in|located in|remote|hybrid|onsite|on-site)[:\s]*([^,.]+(?:,\s*[^,.]+)*)',
                prompt, re.IGNORECASE
            )
            if location_match:
                ad_fields["location"] = location_match.group(0).strip()
            elif "remote" in prompt_lower:
                ad_fields["location"] = "Remote"

            # Detect salary from prompt
            salary_match = re.search(
                r'(?:salary|pay|compensation)[:\s]*([^.]+)',
                prompt, re.IGNORECASE
            )
            if salary_match:
                ad_fields["salary"] = salary_match.group(1).strip()

            session["job_ad_fields"] = ad_fields

            # If we have platforms, go straight to generation
            if ad_fields.get("platforms"):
                yield from self._build_and_call_job_ad(session_id, session)
                return

            # Otherwise, ask ONLY for platforms
            title_display = job_title or "that role"
            follow_up = (
                f"I'll create a job ad for **{title_display}**! 🎯\n\n"
                "Which **platform(s)** should I create the ad for?\n"
                "- **Facebook** — casual, social media style\n"
                "- **LinkedIn** — professional, detailed\n"
                "- **Indeed** — job board format\n"
                "- **All** — generate for all platforms\n\n"
                "Just type the platform name(s)."
            )
            memory.add_message(session_id, "assistant", follow_up)
            yield {"type": "content", "value": follow_up}
            return


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
                session["last_output_type"] = "job_description"

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