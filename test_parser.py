import sys
import io

if sys.platform == "win32":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', line_buffering=True)

from parser import parse_questions_message

def test_valid_bilingual_txt():
    sample_txt = """
मौर्य वंश का काल-खंड क्या था? / What was the time period of the Mauryan Dynasty?
321-185 ईसा पूर्व / 321-185 BCE ✅
500-200 ईसा पूर्व / 500-200 BCE
273-232 ईसा पूर्व / 273-232 BCE
185-73 ईसा पूर्व / 185-73 BCE

मौर्य साम्राज्य की राजधानी कौन सी थी? / Which was the capital of the Mauryan Empire?
पाटलिपुत्र / Pataliputra ✅
उज्जैन / Ujjain
तक्षशिला / Taxila
मथुरा / Mathura
"""
    questions, errors = parse_questions_message(sample_txt)
    print(f"Parsed {len(questions)} questions. Errors: {len(errors)}")
    assert len(questions) == 2, f"Expected 2 questions, got {len(questions)}"
    assert len(errors) == 0, f"Expected 0 errors, got {errors}"
    assert questions[0]["correct_option_id"] == 0
    assert questions[1]["correct_option_id"] == 0
    assert "✅" not in questions[0]["options"][0]
    print("[SUCCESS] test_valid_bilingual_txt passed!")

def test_invalid_option_count():
    sample_txt = """
Invalid Question Title
Option 1
Option 2 ✅
Option 3
"""
    questions, errors = parse_questions_message(sample_txt)
    assert len(questions) == 0
    assert len(errors) > 0
    print("[SUCCESS] test_invalid_option_count passed!")

def test_missing_correct_mark():
    sample_txt = """
Question Title
Option 1
Option 2
Option 3
Option 4
"""
    questions, errors = parse_questions_message(sample_txt)
    assert len(questions) == 0
    assert len(errors) > 0
    print("[SUCCESS] test_missing_correct_mark passed!")

if __name__ == "__main__":
    print("Running parser unit tests...")
    test_valid_bilingual_txt()
    test_invalid_option_count()
    test_missing_correct_mark()
    print("[SUCCESS] All parser tests passed successfully!")
