import importlib.util
from pathlib import Path


SCRIPT = Path(__file__).parents[1] / "scripts" / "render_review_page.py"
SPEC = importlib.util.spec_from_file_location("render_review_page", SCRIPT)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


def test_scoreboard_orders_models_and_counts_strict_gates():
    items = [
        {
            "difficulty": {"tier": "hard"},
            "task": {
                "model_results": {
                    "model:gemini-3.5-flash": {
                        "resolved": True,
                        "generation_apply": True,
                        "correctness_ok": True,
                        "maintainability": "yes",
                    },
                    "model:claude-opus-4.8": {
                        "resolved": False,
                        "generation_apply": True,
                        "correctness_ok": True,
                        "maintainability": "no",
                    },
                    "model:gpt-5.5": {
                        "resolved": False,
                        "generation_apply": False,
                        "correctness_ok": False,
                        "maintainability": None,
                    },
                }
            },
        }
    ]

    html = MODULE.render_scoreboard(items)

    assert html.index("Gemini-3.5-Flash") < html.index("Claude-Opus-4.8")
    assert "<h2>1 题三模型总榜</h2>" in html
    assert "<strong>1</strong><span>/1 Resolved@1</span>" in html
    assert "总榜第一" in html
    assert "Hard 第一" in html
