import re
from contextlib import redirect_stdout
from dataclasses import dataclass
from enum import Enum
from io import StringIO
from typing import Optional

import json5
from IPython import InteractiveShell
from timeout_decorator import timeout as timer

from ..schema import ActionReturn, ActionStatusCode
from .base_action import BaseAction, tool_api
from .parser import BaseParser, JsonParser


class Status(str, Enum):
    SUCCESS = 'success'
    FAILURE = 'failure'


@dataclass
class ExecutionResult:
    status: Status
    value: Optional[str] = None
    msg: Optional[str] = None


class IPythonInteractive(BaseAction):
    """An interactive IPython shell for code execution.

    Args:
        description (str): The description of the action. Defaults to
            :py:data:`DEFAULT_DESCRIPTION`.
        name (str, optional): The name of the action. If None, the name will
            be class nameDefaults to ``None``.
        enable (bool, optional): Whether the action is enabled. Defaults to
            ``True``.
        disable_description (str, optional): The description of the action when
            it is disabled. Defaults to ``None``.
        timeout (int): Upper bound of waiting time for Python script execution.
            Defaults to ``20``.
        max_out_len (int): maximum output length. No truncation occurs if negative.
            Defaults to ``2048``.
        use_signals (bool): whether signals should be used for timing function out
            or the multiprocessing. Set to ``False`` when not running in the main 
            thread, e.g. web applications. Defaults to ``True``
    """

    def __init__(
        self,
        timeout: int = 30,
        max_out_len: int = 2048,
        use_signals: bool = True,
        description: Optional[dict] = None,
        parser: type[BaseParser] = JsonParser,
        enable: bool = True,
    ) -> None:
        super().__init__(description, parser, enable)
        self.timeout = timeout
        self._executor = InteractiveShell()
        self._highlighting = re.compile(r'\x1b\[\d{,3}(;\d{,3}){,3}m')
        self._max_out_len = max_out_len if max_out_len >= 0 else None
        self._use_signals = use_signals

    def reset(self):
        """Clear the context"""
        self._executor.reset()

    @tool_api
    def run(self, command: str, timeout: Optional[int] = None) -> ActionReturn:
        """启动IPython Interactive Shell用于执行Python代码。

        Args:
            command (:class:`str`): Python code snippet
            timeout (:class:`Optional[int]`): timeout for execution. This 
                argument only works in the main thread. Defaults to ``None``.
        """
        tool_return = ActionReturn(url=None, args=None, type=self.name)
        tool_return.args = {'text': command}
        ret = (
            timer(timeout or self.timeout)(self.exec)(command)
            if self._use_signals else self.exec(command))
        if ret.status is Status.SUCCESS:
            tool_return.result = [{'type': 'text', 'content': ret.value}]
            tool_return.state = ActionStatusCode.SUCCESS
        else:
            tool_return.errmsg = ret.msg
            tool_return.state = ActionStatusCode.API_ERROR
        return tool_return

    def exec(self, code: str) -> ExecutionResult:
        """Run Python scripts in IPython shell

        Args:
            code (:class:`str`): code block

        Returns:
            :py:class:`ExecutionResult`: execution result
        """
        with StringIO() as io:
            with redirect_stdout(io):
                ret = self._executor.run_cell(self.extract_code(code))
                result = ret.result
                if result is not None:
                    return ExecutionResult(Status.SUCCESS,
                                           str(result)[:self._max_out_len])
            outs = io.getvalue().strip().split('\n')
        if not outs:
            return ExecutionResult(Status.SUCCESS, '')
        for i, out in enumerate(outs):
            if re.search('Error|Traceback', out, re.S):
                if 'TimeoutError' in out:
                    return ExecutionResult(
                        Status.FAILURE,
                        msg=
                        'The code interpreter encountered an unexpected error.'
                    )
                err_idx = i
                break
        else:
            return ExecutionResult(Status.SUCCESS,
                                   outs[-1].strip()[:self._max_out_len])
        return ExecutionResult(
            Status.FAILURE,
            msg=self._highlighting.sub(
                '', '\n'.join(outs[err_idx:])[:self._max_out_len]),
        )

    async def async_exec(self, code: str) -> ExecutionResult:
        """Asynchronously run Python scripts in IPython shell

        Args:
            code (:class:`str`): code block

        Returns:
            :py:class:`ExecutionResult`: execution result
        """
        with StringIO() as io:
            with redirect_stdout(io):
                ret = await self._executor.run_cell_async(
                    self.extract_code(code))
                result = ret.result
                if result is not None:
                    return ExecutionResult(Status.SUCCESS,
                                           str(result)[:self._max_out_len])
            outs = io.getvalue().strip().split('\n')
        if not outs:
            return ExecutionResult(Status.SUCCESS, '')
        for i, out in enumerate(outs):
            if re.search('Error|Traceback', out, re.S):
                if 'TimeoutError' in out:
                    return ExecutionResult(
                        Status.FAILURE,
                        msg=
                        'The code interpreter encountered an unexpected error.'
                    )
                err_idx = i
                break
        else:
            return ExecutionResult(Status.SUCCESS,
                                   outs[-1].strip()[:self._max_out_len])
        return ExecutionResult(
            Status.FAILURE,
            msg=self._highlighting.sub(
                '', '\n'.join(outs[err_idx:])[:self._max_out_len]),
        )

    @staticmethod
    def extract_code(text: str) -> str:
        """Extract Python code from markup languages

        Args:
            text (:class:`str`): Markdown-formatted text

        Returns:
            :class:`str`: Python code
        """
        # Match triple backtick blocks first
        triple_match = re.search(r'```[^\n]*\n(.+?)```', text, re.DOTALL)
        # Match single backtick blocks second
        single_match = re.search(r'`([^`]*)`', text, re.DOTALL)
        if triple_match:
            text = triple_match.group(1)
        elif single_match:
            text = single_match.group(1)
        else:
            try:
                text = json5.loads(text)['code']
            except Exception:
                pass
        # If no code blocks found, return original text
        return text
