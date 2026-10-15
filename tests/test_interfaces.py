import asyncio
import json
import os
import sys
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.shared.memory import create_connected_server_and_client_session

from debt_payoff_planner.cli import main
from debt_payoff_planner.config import make_planner
from debt_payoff_planner.mcp_server import create_server
from debt_payoff_planner.web.app import create_app


def cli(capsys, tmp_path, *args):
    status = main(["--data-dir", str(tmp_path / "cli"), *args, "--json"])
    output = capsys.readouterr()
    return status, json.loads(output.out)


def test_all_cli_commands(capsys, tmp_path, examples):
    assert cli(capsys, tmp_path, "init")[0] == 0
    assert (
        cli(capsys, tmp_path, "import", str(examples / "five_debts.json"))[1]["data"]["inserted"]
        == 6
    )
    assert len(cli(capsys, tmp_path, "list")[1]["data"]) == 5
    assert (
        cli(
            capsys,
            tmp_path,
            "add",
            "--code",
            "EXTRA",
            "--type",
            "personal",
            "--balance",
            "10",
            "--start-date",
            "2026-10-01",
            "--minimum",
            "1",
        )[0]
        == 0
    )
    assert cli(capsys, tmp_path, "update", "EXTRA", "--data", '{"priority":0}')[0] == 0
    plan = cli(
        capsys,
        tmp_path,
        "simulate",
        "--strategy",
        "avalanche",
        "--budget",
        "600",
        "--start",
        "2026-10",
        "--save",
    )[1]["data"]
    assert plan["complete"] and plan["plan_id"]
    payment = cli(
        capsys,
        tmp_path,
        "pay",
        "EXTRA",
        "--amount",
        "10",
        "--date",
        "2026-10-10",
        "--interest",
        "0",
        "--idempotency-key",
        "actual-payment",
    )[1]["data"]
    assert payment["balance_cents"] == 0
    assert len(cli(capsys, tmp_path, "payments")[1]["data"]) == 1
    assert cli(capsys, tmp_path, "budget", "set", "2027-01", "--amount", "800")[0] == 0
    assert cli(capsys, tmp_path, "budget", "list")[1]["data"]["2027-01"] == 80000
    assert cli(capsys, tmp_path, "compare", "--budget", "600", "--start", "2026-10")[1]["data"][
        "lowest_interest_strategy"
    ]
    assert cli(
        capsys,
        tmp_path,
        "extra",
        "--budget",
        "600",
        "--amount",
        "100",
        "--month",
        "2026-12",
        "--start",
        "2026-10",
    )[1]["data"]["candidates"]
    assert cli(capsys, tmp_path, "due", "--within", "7d")[0] == 0
    assert cli(capsys, tmp_path, "plans")[1]["data"]
    assert cli(capsys, tmp_path, "progress", "--plan", plan["plan_id"])[0] == 0
    assert cli(capsys, tmp_path, "apr", "--fee-rate", "0.6%", "--months", "12")[1]["data"][
        "monthly_irr"
    ]
    assert (
        cli(capsys, tmp_path, "apr", "--daily-rate", "0.05%")[1]["data"]["nominal_annual_rate"]
        == "0.1825"
    )
    assert cli(capsys, tmp_path, "report", "--md", "--budget", "600", "--start", "2026-10")[1][
        "data"
    ]["charts"]
    assert (
        cli(capsys, tmp_path, "export", "--format", "csv", "--out", str(tmp_path / "all.csv"))[0]
        == 0
    )
    assert cli(capsys, tmp_path, "backup")[1]["data"]["bytes"] > 0


@pytest.mark.parametrize(
    "args,status",
    [
        (["unknown"], 2),
        (["pay", "missing", "--amount", "10", "--date", "2026-10-01"], 3),
        (["simulate"], 2),
        (["due", "--within", "bad"], 2),
        (["import", "/missing/snapshot.json"], 1),
        (["add", "--code", "X", "--type", "personal", "--balance", "0"], 2),
    ],
)
def test_cli_errors(capsys, tmp_path, args, status):
    code, result = cli(capsys, tmp_path, *args)
    assert code == status and not result["ok"] and result["error"]["message"]


def test_config_globals_and_environment(capsys, tmp_path, monkeypatch):
    assert main(["--version"]) == 0
    assert capsys.readouterr().out.strip() == "0.1.0"
    assert main(["--help"]) == 0
    assert "Global:" in capsys.readouterr().out
    config = tmp_path / "settings.yaml"
    config.write_text("data_dir: configured\ndefault_budget: '800'\n")
    assert make_planner(config=str(config)).data_dir == tmp_path / "configured"
    monkeypatch.setenv("DEBT_DATA_DIR", str(tmp_path / "env"))
    assert main(["init", "--quiet"]) == 0
    assert capsys.readouterr().out == ""
    assert main(["init", "--json", "--progress"]) == 0
    output = capsys.readouterr()
    assert json.loads(output.out)["data"]["database"].endswith("env/debts.db")
    assert "Running" in output.err
    config.write_text("unknown: true")
    with pytest.raises(ValueError):
        make_planner(config=str(config))


def test_rest_dashboard_and_security(planner, examples):
    planner.import_file(examples / "five_debts.json")
    client = TestClient(create_app(planner), base_url="http://127.0.0.1")
    assert client.get("/").status_code == 200
    assert client.get("/static/app.js").status_code == 200
    assert client.get("/api/status").json()["data"]["currency"] == "CNY"
    assert len(client.get("/api/debts").json()["data"]) == 5
    assert (
        client.get("/api/compare?budget=600&start=2026-10").json()["data"]["strategies"][
            "avalanche"
        ]["total_interest_cents"]
        == 112882
    )
    saved = client.post(
        "/api/plans", json={"strategy": "avalanche", "budget": "600", "start": "2026-10"}
    ).json()["data"]
    assert client.get("/api/plans").json()["data"][0]["id"] == saved["plan_id"]
    assert client.get("/api/progress/" + saved["plan_id"]).status_code == 200
    payment = {
        "debt_id": "PERSONAL-E",
        "date": "2026-10-10",
        "amount": "100",
        "interest": "0",
        "idempotency_key": "api-payment",
    }
    assert client.post("/api/payments", json=payment).json()["data"]["principal_cents"] == 10000
    assert len(client.get("/api/payments").json()["data"]) == 1
    assert client.get("/api/due").status_code == 200
    assert client.get("/api/apr?fee_rate=0.006&months=12").json()["data"]["monthly_irr"]
    assert (
        "Debt payoff comparison"
        in client.get("/api/report?budget=600&start=2026-10").json()["data"]["markdown"]
    )
    assert client.get("/api/progress/missing").status_code == 404
    assert client.get("/api/compare?months=bad").json()["error"]["code"] == "INVALID_ARGUMENT"
    assert client.get("/api/compare?budget=-1").status_code == 400
    assert (
        client.post(
            "/api/payments", json=payment, headers={"Origin": "https://untrusted.invalid"}
        ).status_code
        == 403
    )
    assert client.get("/api/debts", headers={"Host": "untrusted.invalid"}).status_code == 400
    assert "/api/payments" in client.get("/openapi.json").json()["paths"]


def payload(result):
    return result.structuredContent or json.loads(result.content[0].text)


def test_every_mcp_tool(planner, examples):
    planner.import_file(examples / "five_debts.json")

    async def scenario():
        async with create_connected_server_and_client_session(
            create_server(planner), read_timeout_seconds=timedelta(seconds=10)
        ) as client:
            await client.initialize()
            assert {t.name for t in (await client.list_tools()).tools} == {
                "list_debts",
                "simulate_plan",
                "compare_strategies",
                "record_payment",
                "upcoming_payments",
            }
            listed = payload(await client.call_tool("list_debts", {}))
            assert len(listed.get("result", listed)) == 5
            assert payload(
                await client.call_tool(
                    "simulate_plan", {"strategy": "avalanche", "budget": "600", "start": "2026-10"}
                )
            )["complete"]
            assert (
                payload(
                    await client.call_tool(
                        "compare_strategies", {"budget": "600", "start": "2026-10"}
                    )
                )["strategies"]["baseline"]["months"]
                == 118
            )
            assert (
                payload(
                    await client.call_tool(
                        "record_payment",
                        {
                            "debt_id": "PERSONAL-E",
                            "amount": "100",
                            "date": "2026-10-10",
                            "interest": "0",
                            "idempotency_key": "mcp-payment",
                        },
                    )
                )["principal_cents"]
                == 10000
            )
            assert "payments" in payload(await client.call_tool("upcoming_payments", {"days": 7}))
            assert (
                await client.call_tool(
                    "record_payment", {"debt_id": "missing", "amount": "1", "date": "2026-10-10"}
                )
            ).isError

    asyncio.run(scenario())


def test_real_stdio(tmp_path):
    async def scenario():
        options = StdioServerParameters(
            command=sys.executable,
            args=["-m", "debt_payoff_planner.cli", "--data-dir", str(tmp_path / "stdio"), "mcp"],
            env=dict(os.environ),
        )
        async with stdio_client(options) as (read, write):
            async with ClientSession(
                read, write, read_timeout_seconds=timedelta(seconds=15)
            ) as client:
                assert (await client.initialize()).serverInfo.name == "debt-payoff-planner"
                assert payload(
                    await client.call_tool("simulate_plan", {"strategy": "baseline", "budget": "0"})
                )["complete"]

    asyncio.run(scenario())
