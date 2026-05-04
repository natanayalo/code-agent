from sandbox.redact import SecretRedactor, construct_sandbox_output, sanitize_command


def test_redaction():
    redactor = SecretRedactor(secrets=["my-secret-key"])

    # Test sanitize_command with URL credentials
    cmd = "git clone https://user:token@github.com/repo.git && echo my-secret-key"
    sanitized = sanitize_command(cmd, redactor)
    print(f"Sanitized: {sanitized}")
    assert "https://****@github.com" in sanitized
    assert "my-secret-key" not in sanitized
    assert "[REDACTED]" in sanitized

    # Test construct_sandbox_output with URL credentials
    stdout = "cloning https://user:token@github.com/repo.git\ndone"
    stderr = "secret is my-secret-key"
    output = construct_sandbox_output(stdout, stderr, redactor)
    print(f"Output:\n{output}")
    assert "https://****@github.com" in output
    assert "my-secret-key" not in output
    assert "[REDACTED]" in output


if __name__ == "__main__":
    test_redaction()
    print("Verification passed!")
