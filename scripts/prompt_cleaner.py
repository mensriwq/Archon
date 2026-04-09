import re
import difflib
import logging

logger = logging.getLogger("prompt_cleaner")

REFERENCE_STR = (
    "You are powered by the model named Sonnet 4.6. The exact model ID is claude-sonnet-4-6.\n"
    " - Assistant knowledge cutoff is August 2025.\n"
    " - The most recent Claude model family is Claude 4.6 and 4.5. Model IDs — Opus 4.6: 'claude-opus-4-6', Sonnet 4.6: 'claude-sonnet-4-6', Haiku 4.5: 'claude-haiku-4-5-20251001'. When building AI applications, default to the latest and most capable Claude models.\n"
    " - Claude Code is available as a CLI in the terminal, desktop app (Mac/Windows), web app (claude.ai/code), and IDE extensions (VS Code, JetBrains).\n"
    " - Fast mode for Claude Code uses the same Claude Opus 4.6 model with faster output. It does NOT switch to a different model. It can be toggled with /fast.\n"
)

IDENTITY_PATTERN = r"(?:\n|\\n|^)\s*-\s*You are powered by the model named .*? toggled with /fast\.(?:\n|\\n|$)"

def clean_system_prompt(prompt_text: str, similarity_threshold: float = 0.8) -> str:
    if not prompt_text:
        return prompt_text

    cleaned_text = prompt_text

    matches = list(re.finditer(IDENTITY_PATTERN, prompt_text, flags=re.DOTALL))
    for match in reversed(matches):
        found_fragment = match.group(0)
        s = difflib.SequenceMatcher(None, REFERENCE_STR.strip(), found_fragment.strip())
        ratio = s.ratio()
        
        if ratio >= similarity_threshold:
            logger.info(f"Successfully cleaned known Identity Declaration (Similarity: {ratio:.2f})")
            cleaned_text = cleaned_text[:match.start()] + cleaned_text[match.end():]
        else:
            logger.warning(
                f"\n[PROMPT CLEANER WARNING] Found potential identity fragment with LOW similarity ({ratio:.2f}). SKIPPING REMOVAL.\n"
                f"--- FOUND ---\n{found_fragment.strip()}\n"
                f"--- EXPECTED ---\n{REFERENCE_STR.strip()}\n"
            )

    return cleaned_text

def clean_payload_system(system_field):
    if isinstance(system_field, str):
        return clean_system_prompt(system_field)
    elif isinstance(system_field, list):
        new_system = []
        for block in system_field:
            if isinstance(block, dict) and block.get("type") == "text":
                new_block = block.copy()
                new_block["text"] = clean_system_prompt(block["text"])
                new_system.append(new_block)
            else:
                new_system.append(block)
        return new_system
    return system_field
