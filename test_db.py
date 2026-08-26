import sys
import io

if sys.platform == "win32":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', line_buffering=True)

import db

def test_sqlite_multi_user_isolation():
    db.init_db()

    user_a_id = 11111
    user_b_id = 22222

    # Save quiz for User A
    quiz_a_id = db.save_quiz(
        user_id=user_a_id,
        name="User A History Quiz",
        timer=15,
        questions=[{"question_text": "Q1", "options": ["A", "B", "C", "D"], "correct_option_id": 0}],
        creator_name="User A"
    )

    # Save quiz for User B
    quiz_b_id = db.save_quiz(
        user_id=user_b_id,
        name="User B Science Quiz",
        timer=20,
        questions=[{"question_text": "Q1", "options": ["A", "B", "C", "D"], "correct_option_id": 1}],
        creator_name="User B"
    )

    # Check isolation
    user_a_quizzes = db.get_user_quizzes(user_a_id)
    user_b_quizzes = db.get_user_quizzes(user_b_id)

    assert any(q["quiz_id"] == quiz_a_id for q in user_a_quizzes), "User A should see Quiz A"
    assert not any(q["quiz_id"] == quiz_b_id for q in user_a_quizzes), "User A must NOT see Quiz B"

    assert any(q["quiz_id"] == quiz_b_id for q in user_b_quizzes), "User B should see Quiz B"
    assert not any(q["quiz_id"] == quiz_a_id for q in user_b_quizzes), "User B must NOT see Quiz A"

    # User B attempts to delete User A's quiz -> Should fail
    deleted = db.delete_quiz(quiz_a_id, user_b_id)
    assert deleted is False, "User B should NOT be able to delete User A's quiz"

    # User A deletes User A's quiz -> Should succeed
    deleted_a = db.delete_quiz(quiz_a_id, user_a_id)
    assert deleted_a is True, "User A should be able to delete their own quiz"

    db.delete_quiz(quiz_b_id, user_b_id)
    print("[SUCCESS] test_sqlite_multi_user_isolation passed successfully!")

if __name__ == "__main__":
    print("Running database multi-user security tests...")
    test_sqlite_multi_user_isolation()
    print("[SUCCESS] All database security tests passed!")
