import sys

if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding='utf-8')
    except Exception:
        pass

from quiz_bot import truncate_text, format_quiz_to_txt
from parser import parse_questions_message

def test_truncation():
    # TEST 1: Short question & options -> normal display (no truncation)
    assert truncate_text("Short Q", 200) == "Short Q"
    assert truncate_text("Short Opt", 40) == "Short Opt"
    
    # TEST 2: Very long question -> truncated with "..." at 200
    long_q = "Q" * 400
    truncated_q = truncate_text(long_q, 200)
    assert len(truncated_q) == 200
    assert truncated_q.endswith("...")
    
    # TEST 3: Very long option -> truncated with "..." at 40
    long_opt = "Opt" * 50
    truncated_opt = truncate_text(long_opt, 40)
    assert len(truncated_opt) == 40
    assert truncated_opt.endswith("...")
    
    print("✅ Truncation tests passed!")

def test_export_import():
    questions = [
        {
            "question_text": "Hindi Question / English Question text with multi lines\nSecond line of question",
            "options": [
                "A) Option A / Option A",
                "B) Option B / Option B",
                "C) Option C / Option C",
                "D) Option D / Option D"
            ],
            "correct_option_id": 2
        },
        {
            "question_text": "Second Question text",
            "options": [
                "1. First Option",
                "2. Second Option"
            ],
            "correct_option_id": 0
        }
    ]
    
    # TEST 6 & 7: Export to TXT format
    txt_content = format_quiz_to_txt(questions)
    assert "Hindi Question" in txt_content
    assert "Option C / Option C ✅" in txt_content
    assert "First Option ✅" in txt_content
    
    # TEST 8, 9 & 10: Import from exported format
    parsed = parse_questions_message(txt_content)
    assert len(parsed) == 2
    assert parsed[0]["question_text"] == questions[0]["question_text"]
    assert parsed[0]["options"] == questions[0]["options"]
    assert parsed[0]["correct_option_id"] == questions[0]["correct_option_id"]
    
    assert parsed[1]["question_text"] == questions[1]["question_text"]
    assert parsed[1]["options"] == questions[1]["options"]
    assert parsed[1]["correct_option_id"] == questions[1]["correct_option_id"]
    
    print("✅ Export and Import integrity tests passed!")

if __name__ == "__main__":
    test_truncation()
    test_export_import()
    print("🎉 ALL NEW FEATURES TESTS PASSED SUCCESSFULLY!")
