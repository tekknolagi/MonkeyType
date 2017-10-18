import enum
import inspect
import logging
import opcode
import random
import sys
from abc import (
    ABCMeta,
    abstractmethod,
)
from contextlib import contextmanager
from types import (
    CodeType,
    FrameType,
)
from typing import (
    Any,
    Callable,
    Dict,
    Optional,
    Union,
    cast,
)

from monkeytype.typing import get_type
from monkeytype.util import get_func_fqname


logger = logging.getLogger(__name__)


class Env(enum.Enum):
    """Env represents where the types were captured. We'll end up capturing types both
    in production and in unit tests. This gives us a way to compare types that are captured in
    different environments.
    """
    TEST = 'test'
    PROD = 'prod'


class CallTrace:
    """CallTrace contains the types observed during a single invocation of a function"""

    def __init__(self,
                 env: Env,
                 func: Callable,
                 arg_types: Dict[str, type],
                 return_type: type = None,
                 yield_type: type = None) -> None:
        """
        Args:
            env: Where the trace was captured
            func: The function where the trace ocurred
            arg_types: The collected argument types
            return_type: The collected return type. This will be None if the called function returns
                due to an unhandled exception. It will be NoneType if the function returns the value None.
            yield_type: The collected yield type. This will be None if the called function never
                yields. It will be NoneType if the function yields the value None.
        """
        self.env = env
        self.func = func
        self.arg_types = arg_types
        self.return_type = return_type
        self.yield_type = yield_type

    def __eq__(self, other):
        if isinstance(other, self.__class__):
            return self.__dict__ == other.__dict__
        return NotImplemented

    def __repr__(self) -> str:
        return "CallTrace(%s, %s, %s, %s, %s)" % (
            self.env, self.func, self.arg_types, self.return_type, self.yield_type)

    def __hash__(self) -> int:
        return hash((self.env, self.func, frozenset(self.arg_types.items()), self.return_type, self.yield_type))

    def add_yield_type(self, typ: type) -> None:
        if self.yield_type is None:
            self.yield_type = typ
        else:
            self.yield_type = Union[self.yield_type, typ]

    @property
    def funcname(self) -> str:
        return get_func_fqname(self.func)


class CallTraceLogger(metaclass=ABCMeta):
    """CallTraceLogger logs (e.g. prints to stdout, logs to scuba) records that are
    collected by a CallTracer.
    """

    @abstractmethod
    def log(self, trace: CallTrace):
        pass


def get_func_in_mro(obj: Any, code: CodeType) -> Optional[Callable]:
    """Attempt to find a function in a side-effect free way.

    This looks in obj's mro manually and does not invoke any descriptors.
    """
    # T17161244 FunctionType is incompatible with Callable
    val = inspect.getattr_static(obj, code.co_name, None)
    if val is None:
        return None
    if isinstance(val, (classmethod, staticmethod)):
        cand = cast(Callable, val.__func__)
    elif isinstance(val, property) and (val.fset is None) and (val.fdel is None):
        cand = cast(Callable, val.fget)
    else:
        cand = cast(Callable, val)
    return _has_code(cand, code)


def _has_code(func: Optional[Callable], code: CodeType) -> Optional[Callable]:
    while func is not None:
        func_code = getattr(func, '__code__', None)
        if func_code is code:
            return func
        # Attempt to find the decorated function
        func = getattr(func, '__wrapped__', None)
    return None


def get_func(frame: FrameType) -> Optional[Callable]:
    """Return the function whose code object corresponds to the supplied stack frame"""
    code = frame.f_code
    if code.co_name is None:
        return None
    # First, try to find the function in globals
    cand = frame.f_globals.get(code.co_name, None)
    func = _has_code(cand, code)
    # If that failed, as will be the case with class and instance methods, try
    # to look up the function from the first argument. In the case of class/instance
    # methods, this should be the class (or an instance of the class) on which our
    # method is defined.
    if func is None and code.co_argcount >= 1:
        first_arg = frame.f_locals.get(code.co_varnames[0])
        func = get_func_in_mro(first_arg, code)
    # If we still can't find the function, as will be the case with static methods,
    # try looking at classes in global scope.
    if func is None:
        for v in frame.f_globals.values():
            if not isinstance(v, type):
                continue
            func = get_func_in_mro(v, code)
            if func is not None:
                break
    return func


RETURN_VALUE_OPCODE = opcode.opmap['RETURN_VALUE']
YIELD_VALUE_OPCODE = opcode.opmap['YIELD_VALUE']


class CallTracer:
    """CallTracer captures the concrete types involved in a function invocation.

    On a per function call basis, CallTracer will record the types of arguments
    supplied, the type of the function's return value (if any), and the types
    of values yielded by the function (if any). It emits a CallTrace object
    that contains the captured types when the function returns.

    Use it like so:

        sys.setprofile(CallTracer(Env.TEST, MyCallLogger()))

    """

    EVENT_CALL = 'call'
    EVENT_RETURN = 'return'
    SUPPORTED_EVENTS = {EVENT_CALL, EVENT_RETURN}

    def __init__(self, env: Env, logger: CallTraceLogger, sample_rate: int = 1) -> None:
        self.env = env
        self.logger = logger
        self.traces: Dict[FrameType, CallTrace] = {}
        self.sample_rate = sample_rate
        self.cache: Dict[CodeType, Optional[Callable]] = {}

    def _get_func(self, frame: FrameType) -> Optional[Callable]:
        code = frame.f_code
        if code not in self.cache:
            self.cache[code] = get_func(frame)
        return self.cache[code]

    def handle_call(self, frame: FrameType) -> None:
        if random.randrange(self.sample_rate) != 0:
            return
        func = self._get_func(frame)
        if func is None:
            return
        code = frame.f_code
        # I can't figure out a way to access the value sent to a generator via send() from a stack frame. Thankfully, it
        # doesn't look like anything in distillery is defined this way.
        if code.co_code[frame.f_lasti] == YIELD_VALUE_OPCODE:
            return
        arg_names = code.co_varnames[0:code.co_argcount]
        arg_types = {}
        for name in arg_names:
            if name in frame.f_locals:
                arg_types[name] = get_type(frame.f_locals[name])
        self.traces[frame] = CallTrace(self.env, func, arg_types)

    def handle_return(self, frame: FrameType, arg: Any) -> None:
        # In the case of a 'return' event, arg contains the return value, or None, if the
        # block returned because of an unhandled exception. We need to distinguish the exceptional
        # case (not a valid return type) from a function returning (or yielding) None. In the latter case,
        # the the last instruction that was executed should always be a return or a yield.
        typ = get_type(arg)
        last_opcode = frame.f_code.co_code[frame.f_lasti]
        trace = self.traces.get(frame)
        if trace is None:
            return
        elif last_opcode == YIELD_VALUE_OPCODE:
            trace.add_yield_type(typ)
        else:
            # NB: In the event that a function returns exceptionally, its
            # trace's return_type will be None.  If the function returns the
            # value None, its trace's return_type will be NoneType.
            if last_opcode == RETURN_VALUE_OPCODE:
                trace.return_type = typ
            del self.traces[frame]
            self.logger.log(trace)

    def __call__(self, frame: FrameType, event: str, arg: Any) -> 'CallTracer':
        code = frame.f_code
        if (
            event not in self.SUPPORTED_EVENTS or
            code.co_name == 'trace_types'
        ):
            return self
        try:
            if event == self.EVENT_CALL:
                self.handle_call(frame)
            elif event == self.EVENT_RETURN:
                self.handle_return(frame, arg)
            else:
                logger.error("Cannot handle event %s", event)
        except Exception:
            logger.exception("Failed collecting trace")
        return self


@contextmanager
def trace_calls(env: Env, logger: CallTraceLogger):
    """Enable call tracing for a block of code"""
    old_trace = sys.getprofile()
    sys.setprofile(CallTracer(env, logger))
    try:
        yield
    finally:
        sys.setprofile(old_trace)