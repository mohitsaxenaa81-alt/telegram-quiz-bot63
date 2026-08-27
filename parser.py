import re

def parse_single_question_block(block_text: str):
    """
    Parses a single block of text containing question lines (Hindi + English) followed by options.
    Returns dict: {"question_text": str, "options": list, "correct_option_id": int} or None if invalid.
    """
    raw_lines = [line.strip() for line in block_text.strip().split("\n") if line.strip()]
    if len(raw_lines) < 3:
        return None

    correct_line_idx = -1
    for i, line in enumerate(raw_lines):
        if "✅" in line:
            if correct_line_idx != -1:
                return None
            correct_line_idx = i

    if correct_line_idx == -1:
        return None

    option_prefix_regex = re.compile(r'^(?:[A-Za-z0-9][\.\)\:]|[\(\[\{][A-Za-z0-9][\)\]\}]|[\u25cb\u2022\u25cf\u25b6\U0001f170-\U0001f189])\s*')

    # Step 1: Check if any line before or at correct_line_idx has an explicit option prefix (excluding line 0 which is question text)
    opt_start_idx = -1
    for i in range(correct_line_idx, 0, -1):
        line_clean = raw_lines[i].replace("✅", "").strip()
        if option_prefix_regex.match(line_clean):
            opt_start_idx = i
        else:
            if opt_start_idx != -1:
                break

    # Step 2: If no option prefixes were found, determine option start index based on block structure
    if opt_start_idx == -1 or opt_start_idx > correct_line_idx:
        possible_start = max(1, correct_line_idx - 3)
        opt_start_idx = possible_start

    question_lines = raw_lines[:opt_start_idx]
    raw_options = raw_lines[opt_start_idx:]

    if not question_lines or len(raw_options) < 2 or len(raw_options) > 10:
        return None

    question_text = "\n".join(question_lines).strip()

    correct_index = -1
    clean_options = []

    for idx, opt in enumerate(raw_options):
        is_correct = False
        if "✅" in opt:
            is_correct = True
            opt = opt.replace("✅", "").strip()

        opt_clean = opt.strip()
        if not opt_clean:
            return None
        clean_options.append(opt_clean)
        if is_correct:
            correct_index = idx

    if correct_index == -1:
        return None

    return {
        "question_text": question_text,  # Full untruncated multi-line question text!
        "options": clean_options,
        "correct_option_id": correct_index
    }

def parse_questions_message(text: str):
    """
    Parses input message which may contain one or multiple question blocks.
    Returns list of question dicts.
    """
    parsed_questions = []
    
    # Try splitting by double linebreaks or question headers if multiple questions sent
    blocks = re.split(r'\n\s*\n+', text.strip())

    for block in blocks:
        q = parse_single_question_block(block)
        if q:
            parsed_questions.append(q)

    # Fallback: if splitting by double linebreaks didn't yield all questions or yielded none,
    # try parsing the full text as a single block if possible.
    if not parsed_questions:
        q = parse_single_question_block(text)
        if q:
            parsed_questions.append(q)

    return parsed_questions
