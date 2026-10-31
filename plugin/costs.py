"""Explicit scenario estimates, separate from measured API usage."""

import math


def monthly_scenarios():
    rows = []
    continuous = []
    for replies in (100, 300, 1000):
        calls = replies * 30
        raw_daily = replies * 600
        batches = math.ceil(raw_daily / 57952)
        daily_memory_peak = (
            30 * ((raw_daily + batches * 12000) * 0.30 + batches * 2500 * 1.20) / 1_000_000
        )
        summaries = max(0, math.ceil((raw_daily * 30 - 216000) / 48000))
        summary_peak = summaries * (72000 * 0.30 + 4000 * 1.20) / 1_000_000
        for hit_rate in (0, 0.8, 0.95):
            chat_peak = (
                calls
                * (30000 * ((1 - hit_rate) * 0.30 + hit_rate * 0.006) + 500 * 1.20)
                / 1_000_000
            )
            rows.append(
                {
                    "replies_per_day": replies,
                    "cache_hit_fraction": hit_rate,
                    "average_input": 30000,
                    "average_output": 500,
                    "chat_peak_usd": round(chat_peak, 4),
                    "chat_offpeak_usd": round(chat_peak / 2, 4),
                    "daily_memory_peak_usd": round(daily_memory_peak, 4),
                    "summary_peak_usd": round(summary_peak, 4),
                    "total_peak_usd": round(chat_peak + daily_memory_peak + summary_peak, 4),
                    "total_offpeak_usd": round(
                        (chat_peak + daily_memory_peak + summary_peak) / 2, 4
                    ),
                }
            )
            deep = dict(rows[-1])
            deep_peak = (
                calls
                * (200000 * ((1 - hit_rate) * 0.30 + hit_rate * 0.006) + 500 * 1.20)
                / 1_000_000
            )
            deep.update(
                average_input=200000,
                chat_peak_usd=round(deep_peak, 4),
                chat_offpeak_usd=round(deep_peak / 2, 4),
                total_peak_usd=round(deep_peak + daily_memory_peak + summary_peak, 4),
                total_offpeak_usd=round((deep_peak + daily_memory_peak + summary_peak) / 2, 4),
            )
            continuous.append(deep)
    return {
        "currency": "USD",
        "days": 30,
        "price_checked": "2026-11-01",
        "assumptions": {
            "model_calls_per_reply": 1,
            "input_tokens_per_reply": 30000,
            "output_tokens_per_reply": 500,
            "new_transcript_tokens_per_reply": 600,
            "daily_batch_output": 2500,
            "summary_output": 4000,
            "jobs_cache_hit_fraction": 0,
            "tool_fees_included": False,
        },
        "scenarios": rows,
        "continuous_scenarios": continuous,
        "sensitivity": [
            {
                "average_input": inp,
                "cache_hit_fraction": hit,
                "monthly_1000_replies_day_peak_usd": round(
                    30000 * (inp * ((1 - hit) * 0.3 + hit * 0.006) + 500 * 1.2) / 1_000_000, 4
                ),
            }
            for inp in (12000, 120000, 240000)
            for hit in (0, 0.8, 0.95)
        ],
    }
