"""Module for executing notebooks."""
import os
from contextlib import nullcontext, suppress
from datetime import datetime
from logging import Logger
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Optional, Tuple

from jupyter_cache import get_cache
from jupyter_cache.base import NbBundleIn
from jupyter_cache.cache.db import NbStageRecord
from jupyter_cache.executors.utils import single_nb_execution
from nbformat import NotebookNode
from typing_extensions import TypedDict

from myst_nb.configuration import NbParserConfig


class ExecutionResult(TypedDict):
    """Result of executing a notebook."""

    mtime: float
    """POSIX timestamp of the execution time"""
    runtime: Optional[float]
    """runtime in seconds"""
    method: str
    """method used to execute the notebook"""
    succeeded: bool
    """True if the notebook executed successfully"""
    # TODO error


def update_notebook(
    notebook: NotebookNode,
    source: str,
    nb_config: NbParserConfig,
    logger: Logger,
) -> Tuple[NotebookNode, Optional[ExecutionResult]]:
    """Update a notebook using the given configuration.

    This function may execute the notebook if necessary.

    :param notebook: The notebook to update.
    :param source: Path to or description of the input source being processed.
    :param nb_config: The configuration for the notebook parser.
    :param logger: The logger to use.

    :returns: The updated notebook, and the (optional) execution metadata.
    """
    # path should only be None when using docutils programmatically
    path = Path(source) if Path(source).is_file() else None

    exec_metadata: Optional[ExecutionResult] = None

    if nb_config.execution_mode == "force":

        # setup the execution current working directory
        if nb_config.execution_in_temp:
            cwd_context = TemporaryDirectory()
        else:
            if path is None:
                raise ValueError(
                    f"source must exist as file, if execution_in_temp=False: {source}"
                )
            cwd_context = nullcontext(str(path.parent))

        # execute in the context of the current working directory
        with cwd_context as cwd:
            cwd = os.path.abspath(cwd)
            logger.info(f"Executing notebook: CWD={cwd!r}")
            result = single_nb_execution(
                notebook,
                cwd=cwd,
                allow_errors=nb_config.execution_allow_errors,
                timeout=nb_config.execution_timeout,
            )
            logger.info(f"Executed notebook in {result.time:.2f} seconds")

        if result.err:
            msg = f"Executing notebook failed: {result.err.__class__.__name__}"
            if nb_config.execution_show_tb:
                msg += f"\n{result.exc_string}"
            logger.warning(msg, subtype="exec")

        exec_metadata = {
            "mtime": datetime.now().timestamp(),
            "runtime": result.time,
            "method": nb_config.execution_mode,
            "succeeded": False if result.err else True,
        }

    elif nb_config.execution_mode == "cache":

        # setup the cache
        cache = get_cache(nb_config.execution_cache_path or ".jupyter_cache")
        # TODO config on what notebook/cell metadata to merge

        # attempt to match the notebook to one in the cache
        cache_record = None
        with suppress(KeyError):
            cache_record = cache.match_cache_notebook(notebook)

        # use the cached notebook if it exists
        if cache_record is not None:
            logger.info(f"Using cached notebook: PK={cache_record.pk}")
            _, notebook = cache.merge_match_into_notebook(notebook)
            exec_metadata = {
                "mtime": cache_record.created.timestamp(),
                "runtime": cache_record.data.get("execution_seconds", None),
                "method": nb_config.execution_mode,
                "succeeded": True,
            }
            return notebook, exec_metadata

        if path is None:
            raise ValueError(
                f"source must exist as file, if execution_mode is 'cache': {source}"
            )

        # attempt to execute the notebook
        stage_record = cache.stage_notebook_file(str(path))  # TODO record nb reader
        # TODO do in try/except, in case of db write errors
        NbStageRecord.remove_tracebacks([stage_record.pk], cache.db)
        cwd_context = (
            TemporaryDirectory()
            if nb_config.execution_in_temp
            else nullcontext(str(path.parent))
        )
        with cwd_context as cwd:
            cwd = os.path.abspath(cwd)
            logger.info(f"Executing notebook: CWD={cwd!r}")
            result = single_nb_execution(
                notebook,
                cwd=cwd,
                allow_errors=nb_config.execution_allow_errors,
                timeout=nb_config.execution_timeout,
            )
            logger.info(f"Executed notebook in {result.time:.2f} seconds")

        # handle success / failure cases
        # TODO do in try/except to be careful (in case of database write errors?
        if result.err:
            msg = f"Executing notebook failed: {result.err.__class__.__name__}"
            if nb_config.execution_show_tb:
                msg += f"\n{result.exc_string}"
            logger.warning(msg, subtype="exec")
            NbStageRecord.set_traceback(stage_record.uri, result.exc_string, cache.db)
        else:
            cache_record = cache.cache_notebook_bundle(
                NbBundleIn(
                    notebook, stage_record.uri, data={"execution_seconds": result.time}
                ),
                check_validity=False,
                overwrite=True,
            )
            logger.info(f"Cached executed notebook: PK={cache_record.pk}")

        exec_metadata = {
            "mtime": datetime.now().timestamp(),
            "runtime": result.time,
            "method": nb_config.execution_mode,
            "succeeded": False if result.err else True,
        }

    return notebook, exec_metadata
