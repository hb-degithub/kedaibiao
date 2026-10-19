# 课代表 kedaibiao —— 常用开发命令
# 核心层零第三方依赖，所以 test / lint 之外的命令都需要先装可选依赖。

PY ?= python3
export PYTHONPATH := src

.PHONY: help test test-v doctor platforms clean fmt lint install install-dev install-browser

help:
	@echo "make test           # 跑全部单测（纯标准库，无需安装依赖）"
	@echo "make test-v         # 同上，详细输出"
	@echo "make doctor         # 环境自检"
	@echo "make platforms      # 列出已注册平台适配器"
	@echo "make install-dev    # 安装开发依赖（pytest/ruff/mypy）"
	@echo "make install-browser# 安装 playwright 与 chromium"
	@echo "make clean          # 清理缓存与运行期数据"

test:
	$(PY) -m unittest discover -s tests -t . -q

test-v:
	$(PY) -m unittest discover -s tests -t . -v

doctor:
	$(PY) -m kedaibiao doctor

platforms:
	$(PY) -m kedaibiao platforms

config:
	$(PY) -m kedaibiao config

install:
	$(PY) -m pip install -e .

install-dev:
	$(PY) -m pip install -e ".[dev]"

install-browser:
	$(PY) -m pip install -e ".[browser]"
	$(PY) -m playwright install chromium

fmt:
	$(PY) -m ruff format src tests
	$(PY) -m ruff check --fix src tests

lint:
	$(PY) -m ruff check src tests
	$(PY) -m mypy src

clean:
	find . -type d -name __pycache__ -prune -exec rm -rf {} +
	rm -rf .pytest_cache .mypy_cache .ruff_cache build dist *.egg-info
