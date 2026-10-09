import replay


def test_offline_replay_runs_all_three_behaviors(capsys):
    assert replay.main([]) == 0
    out = capsys.readouterr().out
    assert "status=200" in out
    assert "status=201" in out and "same_order=True" in out
    assert "status=409" in out
    assert "body tampered        http=401" in out
    assert "same uuid again      http=200  stored=1" in out
    assert out.strip().endswith("all three behaviors held")


def test_live_without_key_exits_2(monkeypatch, capsys):
    monkeypatch.delenv("TREMENDOUS_API_KEY", raising=False)
    monkeypatch.setattr(replay, "load_dotenv", lambda path: None)
    assert replay.main(["--live"]) == 2
    assert "TREMENDOUS_API_KEY" in capsys.readouterr().err
