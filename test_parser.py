import sys
import io

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')

from parser import parse_questions_message

test_text = """
भारतीय संविधान में मौलिक अधिकारों का उद्देश्य नागरिकों की स्वतंत्रता, समानता और गरिमा की रक्षा करना है। संविधान के भाग III में मौलिक अधिकारों का उल्लेख किया गया है और इन अधिकारों के उल्लंघन की स्थिति में नागरिक न्यायालय की सहायता प्राप्त कर सकते हैं। निम्नलिखित में से कौन-सा अधिकार नागरिकों को अपने
Which right allows citizens to move court in case of violation of Fundamental Rights?
समानता का अधिकार / Right to Equality
स्वतंत्रता का अधिकार / Right to Freedom
धार्मिक स्वतंत्रता का अधिकार / Right to Freedom of Religion
संवैधानिक उपचार का अधिकार / Right to Constitutional Remedies ✅
"""

parsed = parse_questions_message(test_text)
print(f"Total parsed questions: {len(parsed)}")
for idx, q in enumerate(parsed, 1):
    print(f"Q{idx}:\n{q['question_text']}")
    print(f"Question Length: {len(q['question_text'])}")
    print(f"Options: {q['options']}")
    print(f"Correct Option Index: {q['correct_option_id']}")
    print("-" * 40)

assert len(parsed) == 1
assert "भारतीय संविधान" in parsed[0]["question_text"]
assert "Which right allows citizens" in parsed[0]["question_text"]
assert len(parsed[0]["question_text"]) > 200
print("ALL MULTI-LINE BILINGUAL PARSER TESTS PASSED SUCCESSFULLY!")
