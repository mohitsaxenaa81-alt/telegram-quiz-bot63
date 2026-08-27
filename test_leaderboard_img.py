import sys
import os

sys.path.insert(0, os.path.dirname(__file__))

import leaderboard_image

test_participants = [
    {"name": "🥀.......🥀", "correct": 148, "wrong": 5, "attempted_set": set(range(153)), "total_time": 120.5},
    {"name": "Asheesh Pandey 🤝", "correct": 146, "wrong": 6, "attempted_set": set(range(152)), "total_time": 130.2},
    {"name": "Savi", "correct": 145, "wrong": 3, "attempted_set": set(range(148)), "total_time": 110.0},
    {"name": "Nibha", "correct": 129, "wrong": 25, "attempted_set": set(range(154)), "total_time": 145.0},
    {"name": "Shivam Samrat", "correct": 124, "wrong": 24, "attempted_set": set(range(148)), "total_time": 140.0},
    {"name": "Aak", "correct": 112, "wrong": 4, "attempted_set": set(range(116)), "total_time": 95.0},
    {"name": "🅿🆈🅳🆁🅰️", "correct": 106, "wrong": 40, "attempted_set": set(range(146)), "total_time": 150.0},
    {"name": "Rëshü..💎", "correct": 101, "wrong": 29, "attempted_set": set(range(130)), "total_time": 125.0},
    {"name": "mahakal", "correct": 95, "wrong": 35, "attempted_set": set(range(130)), "total_time": 135.0},
    {"name": "Anaya", "correct": 94, "wrong": 22, "attempted_set": set(range(116)), "total_time": 115.0},
]

try:
    buf = leaderboard_image.generate_leaderboard_image(test_participants, "Sudharshan Chakra Series", max_rows=10)
    print(f"SUCCESS: Generated leaderboard image buffer size: {len(buf.getvalue())} bytes")
except Exception as e:
    print(f"ERROR: {e}")
