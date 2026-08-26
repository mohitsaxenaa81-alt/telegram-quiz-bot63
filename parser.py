import re
from typing import List, Dict, Any, Tuple

def parse_single_question_block(block_text: str, block_num: int = 1) -> Tuple[Dict[str, Any] | None, str | None]:
    """
    Parses a single block of text containing question lines (Hindi + English) followed by 4 options.
    Returns (question_dict, error_message).
    If valid, error_message is None. If invalid, question_dict is None and error_message describes the problem.
    """
    raw_lines = [line.strip() for line in block_text.strip().split("\n") if line.strip()]
    if not raw_lines:
        return None, f"Block #{block_num} is empty."

    # First, scan for correct answer markers (✅)
    correct_lines = [i for i, line in enumerate(raw_lines) if "✅" in line]

    if len(correct_lines) == 0:
        sample_text = raw_lines[0][:40] + ("..." if len(raw_lines[0]) > 40 else "")
        return None, f"⚠️ Question #{block_num} ('{sample_text}'): Correct answer mark (✅) is missing."

    if len(correct_lines) > 1:
        sample_text = raw_lines[0][:40] + ("..." if len(raw_lines[0]) > 40 else "")
        return None, f"⚠️ Question #{block_num} ('{sample_text}'): Multiple options have correct answer mark (✅). Exactly 1 option must have ✅."

    correct_line_idx = correct_lines[0]

    # Regex for standard option prefixes like A., 1), (a), etc.
    option_prefix_regex = re.compile(
        r'^(?:[A-Za-z0-9][\.\)\:]|[\(\[\{][A-Za-z0-9][\)\]\}]|[\u25cb\u2022\u25cf\u25b6\U0001f170-\U0001f189])\s*'
    )

    # Step 1: Detect start of options
    opt_start_idx = -1
    for i in range(correct_line_idx, 0, -1):
        line_clean = raw_lines[i].replace("✅", "").strip()
        if option_prefix_regex.match(line_clean):
            opt_start_idx = i
        else:
            if opt_start_idx != -1:
                break

    # Step 2: If no explicit prefixes, infer options start index
    # We expect 4 options, so options should start at max(1, len(raw_lines) - 4) or max(1, correct_line_idx - 3)
    if opt_start_idx == -1 or opt_start_idx > correct_line_idx:
        # Check if the block has 5 or more lines, options are likely the last 4 lines or starting near correct_line_idx
        possible_start = max(1, len(raw_lines) - 4)
        if correct_line_idx < possible_start:
            possible_start = max(1, correct_line_idx - 3)
        opt_start_idx = possible_start

    question_lines = raw_lines[:opt_start_idx]
    raw_options = raw_lines[opt_start_idx:]

    if not question_lines:
        return None, f"⚠️ Question #{block_num}: Missing question text."

    question_text = "\n".join(question_lines).strip()
    sample_text = question_lines[0][:40] + ("..." if len(question_lines[0]) > 40 else "")

    if len(raw_options) != 4:
        return None, f"⚠️ Question #{block_num} ('{sample_text}'): Found {len(raw_options)} options instead of exactly 4."

    correct_index = -1
    clean_options = []

    for idx, opt in enumerate(raw_options):
        is_correct = False
        if "✅" in opt:
            is_correct = True
            opt = opt.replace("✅", "").strip()

        # Remove optional leading bullet points or option letters (e.g. "A. ", "1) ") if present
        opt_clean = opt.strip()
        if not opt_clean:
            return None, f"⚠️ Question #{block_num} ('{sample_text}'): Option {idx + 1} is empty."

        clean_options.append(opt_clean)
        if is_correct:
            correct_index = idx

    if correct_index == -1:
        return None, f"⚠️ Question #{block_num} ('{sample_text}'): Correct answer mark (✅) could not be mapped to an option."

    return {
        "question_text": question_text,
        "options": clean_options,
        "correct_option_id": correct_index
    }, None

def parse_questions_message(text: str) -> Tuple[List[Dict[str, Any]], List[str]]:
    """
    Parses input text or .txt file content which may contain one or multiple question blocks.
    Returns tuple: (list_of_valid_question_dicts, list_of_error_strings)
    """
    parsed_questions = []
    errors = []

    if not text or not text.strip():
        return [], ["⚠️ Empty content provided."]

    # Split by one or more blank lines
    raw_blocks = re.split(r'\n\s*\n+', text.strip())

    for idx, block in enumerate(raw_blocks, start=1):
        if not block.strip():
            continue
        q, err = parse_single_question_block(block, block_num=idx)
        if q:
            parsed_questions.append(q)
        else:
            if err:
                errors.append(err)

    # Fallback: If split by blank lines resulted in no questions and no errors (or 1 block), try whole text as 1 block
    if not parsed_questions and not errors:
        q, err = parse_single_question_block(text, block_num=1)
        if q:
            parsed_questions.append(q)
        elif err:
            errors.append(err)

    return parsed_questions, errors
