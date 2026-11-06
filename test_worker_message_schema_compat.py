from __future__ import annotations

import unittest

from opencode_go_worker_core import (
    _extract_text,
    _is_assistant_message,
    _message_info,
    _message_text,
    _token_totals,
)


class WorkerMessageSchemaCompatibilityTests(unittest.TestCase):
    def test_legacy_info_parts_format_is_supported(self):
        message = {
            "info": {
                "id": "legacy-1",
                "role": "assistant",
                "finish": "stop",
                "tokens": {
                    "input": 10,
                    "output": 5,
                    "reasoning": 2,
                    "cache": {"read": 3, "write": 1},
                },
                "cost": 0.01,
            },
            "parts": [
                {"type": "text", "text": "Legacy response"},
            ],
        }

        self.assertTrue(_is_assistant_message(message))
        self.assertEqual(
            _message_text(message),
            "Legacy response",
        )
        self.assertEqual(
            _message_info(message)["finish"],
            "stop",
        )

    def test_v2_root_level_message_is_supported(self):
        message = {
            "id": "v2-1",
            "type": "assistant",
            "finish": "stop",
            "tokens": {
                "input": 20,
                "output": 8,
                "reasoning": 3,
                "cache": {"read": 7, "write": 2},
            },
            "cost": 0.02,
            "content": [
                {"type": "reasoning", "text": "Do not expose reasoning"},
                {"type": "text", "text": "V2 visible response"},
            ],
        }

        self.assertTrue(_is_assistant_message(message))
        self.assertEqual(
            _message_text(message),
            "V2 visible response",
        )
        self.assertEqual(
            _message_info(message)["finish"],
            "stop",
        )

    def test_v2_content_string_is_supported(self):
        message = {
            "type": "assistant",
            "content": "Plain text response",
        }
        self.assertEqual(
            _message_text(message),
            "Plain text response",
        )

    def test_v2_content_object_is_supported(self):
        message = {
            "type": "assistant",
            "content": {
                "type": "text",
                "text": "Single content object",
            },
        }
        self.assertEqual(
            _message_text(message),
            "Single content object",
        )

    def test_reasoning_is_not_returned_as_visible_answer(self):
        content = [
            {"type": "reasoning", "text": "Private reasoning segment"},
            {"type": "tool", "text": "Tool payload"},
            {"type": "text", "text": "Visible answer"},
        ]
        self.assertEqual(
            _extract_text(content),
            "Visible answer",
        )

    def test_old_and_new_messages_are_aggregated_together(self):
        messages = [
            {
                "info": {
                    "role": "assistant",
                    "tokens": {
                        "input": 10,
                        "output": 5,
                        "reasoning": 2,
                        "cache": {"read": 3, "write": 1},
                    },
                    "cost": 0.01,
                },
                "parts": [{"type": "text", "text": "old"}],
            },
            {
                "type": "assistant",
                "tokens": {
                    "input": 20,
                    "output": 8,
                    "reasoning": 3,
                    "cache": {"read": 7, "write": 2},
                },
                "cost": 0.02,
                "content": [{"type": "text", "text": "new"}],
            },
            {
                "type": "user",
                "tokens": {
                    "input": 999,
                    "output": 999,
                    "reasoning": 999,
                },
                "cost": 999,
                "content": [{"type": "text", "text": "user prompt"}],
            },
        ]

        totals = _token_totals(messages)

        self.assertEqual(totals["input"], 30)
        self.assertEqual(totals["output"], 13)
        self.assertEqual(totals["reasoning"], 5)
        self.assertEqual(totals["cache_read"], 10)
        self.assertEqual(totals["cache_write"], 3)
        self.assertAlmostEqual(totals["cost_usd"], 0.03)
        self.assertEqual(totals["assistant_messages"], 2)

    def test_flat_user_message_is_not_counted(self):
        totals = _token_totals([
            {
                "type": "user",
                "tokens": {"input": 100, "output": 0},
                "cost": 0,
            }
        ])

        self.assertEqual(totals["assistant_messages"], 0)
        self.assertEqual(totals["input"], 0)

    def test_v2_finish_is_available_through_normalized_info(self):
        message = {
            "id": "v2-finish",
            "type": "assistant",
            "finish": "stop",
        }
        self.assertEqual(_message_info(message)["finish"], "stop")

    def test_flat_assistant_without_content_is_still_recognized(self):
        message = {
            "id": "v2-empty",
            "type": "assistant",
            "finish": "stop",
        }
        self.assertTrue(_is_assistant_message(message))
        self.assertEqual(_message_text(message), "")


if __name__ == "__main__":
    unittest.main(verbosity=2)
