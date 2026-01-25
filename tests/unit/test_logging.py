"""Unit tests for core/logging.py."""

import logging
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest

from core.logging import get_logger, setup_logging


class TestSetupLogging:
    """Tests for setup_logging function."""

    def test_setup_logging_default_level(self):
        """Test logging setup with default INFO level."""
        with patch("logging.basicConfig") as mock_config:
            setup_logging()
            mock_config.assert_called_once()
            call_kwargs = mock_config.call_args[1]
            assert call_kwargs["level"] == logging.INFO

    def test_setup_logging_debug_level(self):
        """Test logging setup with DEBUG level."""
        with patch("logging.basicConfig") as mock_config:
            setup_logging(level="DEBUG")
            mock_config.assert_called_once()
            call_kwargs = mock_config.call_args[1]
            assert call_kwargs["level"] == logging.DEBUG

    def test_setup_logging_warning_level(self):
        """Test logging setup with WARNING level."""
        with patch("logging.basicConfig") as mock_config:
            setup_logging(level="WARNING")
            mock_config.assert_called_once()
            call_kwargs = mock_config.call_args[1]
            assert call_kwargs["level"] == logging.WARNING

    def test_setup_logging_custom_format(self):
        """Test logging setup with custom format string."""
        custom_format = "%(levelname)s: %(message)s"
        with patch("logging.basicConfig") as mock_config:
            setup_logging(format_string=custom_format)
            mock_config.assert_called_once()
            call_kwargs = mock_config.call_args[1]
            assert call_kwargs["format"] == custom_format

    def test_setup_logging_default_format(self):
        """Test logging setup uses default format when none provided."""
        with patch("logging.basicConfig") as mock_config:
            setup_logging()
            mock_config.assert_called_once()
            call_kwargs = mock_config.call_args[1]
            assert "%(asctime)s" in call_kwargs["format"]
            assert "%(name)s" in call_kwargs["format"]
            assert "%(levelname)s" in call_kwargs["format"]
            assert "%(message)s" in call_kwargs["format"]

    def test_setup_logging_with_file(self):
        """Test logging setup with file output."""
        with tempfile.TemporaryDirectory() as tmpdir:
            log_file = Path(tmpdir) / "test.log"
            setup_logging(log_file=log_file)

            # Verify file handler was created (file should exist after setup)
            assert log_file.exists()

    def test_setup_logging_creates_log_directory(self):
        """Test that setup_logging creates parent directories for log file."""
        with tempfile.TemporaryDirectory() as tmpdir:
            log_file = Path(tmpdir) / "subdir" / "nested" / "test.log"
            setup_logging(log_file=log_file)

            # Verify parent directories were created
            assert log_file.parent.exists()

    def test_setup_logging_force_override(self):
        """Test that setup_logging uses force=True to override existing config."""
        with patch("logging.basicConfig") as mock_config:
            setup_logging()
            mock_config.assert_called_once()
            call_kwargs = mock_config.call_args[1]
            assert call_kwargs.get("force") is True


class TestGetLogger:
    """Tests for get_logger function."""

    def test_get_logger_returns_logger(self):
        """Test that get_logger returns a Logger instance."""
        logger = get_logger("test_module")
        assert isinstance(logger, logging.Logger)

    def test_get_logger_with_name(self):
        """Test that get_logger returns logger with correct name."""
        logger = get_logger("my.module.name")
        assert logger.name == "my.module.name"

    def test_get_logger_returns_same_instance(self):
        """Test that get_logger returns the same instance for same name."""
        logger1 = get_logger("same_name")
        logger2 = get_logger("same_name")
        assert logger1 is logger2

    def test_get_logger_different_names_different_instances(self):
        """Test that different names return different logger instances."""
        logger1 = get_logger("name1")
        logger2 = get_logger("name2")
        assert logger1 is not logger2
