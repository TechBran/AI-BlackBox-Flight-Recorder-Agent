import subprocess

def test_no_hardcoded_whisper1_outside_config():
    """The only 'whisper-1' literal allowed is the STT_MODEL default in config.py.
    Everything else must read the model from config (Brandon: swap the model name,
    not the architecture)."""
    out = subprocess.run(
        ["grep", "-rn", "whisper-1", "Orchestrator", "--include=*.py",
         "--exclude-dir=venv"],
        capture_output=True, text=True, cwd="/home/ai-black-box-fc/Desktop/blackbox_poc./blackbox_poc",
    ).stdout
    offenders = [
        line for line in out.splitlines()
        if "config.py" not in line and "/tests/" not in line and "test_" not in line
    ]
    assert offenders == [], "hardcoded whisper-1 found:\n" + "\n".join(offenders)
