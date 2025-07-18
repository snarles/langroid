import json
import logging
from abc import ABC, abstractmethod
from datetime import datetime
from enum import Enum
from typing import (
    Any,
    Awaitable,
    Callable,
    Dict,
    List,
    Literal,
    Optional,
    Tuple,
    Type,
    Union,
    cast,
)

from langroid.cachedb.base import CacheDBConfig
from langroid.cachedb.redis_cachedb import RedisCacheConfig
from langroid.language_models.model_info import ModelInfo, get_model_info
from langroid.parsing.agent_chats import parse_message
from langroid.parsing.file_attachment import FileAttachment
from langroid.parsing.parse_json import parse_imperfect_json, top_level_json_field
from langroid.prompts.dialog import collate_chat_history
from langroid.pydantic_v1 import BaseModel, BaseSettings, Field
from langroid.utils.configuration import settings
from langroid.utils.output.printing import show_if_debug

logger = logging.getLogger(__name__)


def noop_fn(*args: List[Any], **kwargs: Dict[str, Any]) -> None:
    pass


async def async_noop_fn(*args: List[Any], **kwargs: Dict[str, Any]) -> None:
    pass


FunctionCallTypes = Literal["none", "auto"]
ToolChoiceTypes = Literal["none", "auto", "required"]
ToolTypes = Literal["function"]

DEFAULT_CONTEXT_LENGTH = 16_000


class StreamEventType(Enum):
    TEXT = 1
    FUNC_NAME = 2
    FUNC_ARGS = 3
    TOOL_NAME = 4
    TOOL_ARGS = 5


class RetryParams(BaseSettings):
    max_retries: int = 5
    initial_delay: float = 1.0
    exponential_base: float = 1.3
    jitter: bool = True


class LLMConfig(BaseSettings):
    """
    Common configuration for all language models.
    """

    type: str = "openai"
    streamer: Optional[Callable[[Any], None]] = noop_fn
    streamer_async: Optional[Callable[..., Awaitable[None]]] = async_noop_fn
    api_base: str | None = None
    formatter: None | str = None
    # specify None if you want to use the full max output tokens of the model
    max_output_tokens: int | None = 8192
    timeout: int = 20  # timeout for API requests
    chat_model: str = ""
    completion_model: str = ""
    temperature: float = 0.0
    chat_context_length: int | None = None
    async_stream_quiet: bool = False  # suppress streaming output in async mode?
    completion_context_length: int | None = None
    # if input length + max_output_tokens > context length of model,
    # we will try shortening requested output
    min_output_tokens: int = 64
    use_completion_for_chat: bool = False  # use completion model for chat?
    # use chat model for completion? For OpenAI models, this MUST be set to True!
    use_chat_for_completion: bool = True
    stream: bool = True  # stream output from API?
    # TODO: we could have a `stream_reasoning` flag here to control whether to show
    # reasoning output from reasoning models
    cache_config: None | CacheDBConfig = RedisCacheConfig()
    thought_delimiters: Tuple[str, str] = ("<think>", "</think>")
    retry_params: RetryParams = RetryParams()

    @property
    def model_max_output_tokens(self) -> int:
        return (
            self.max_output_tokens or get_model_info(self.chat_model).max_output_tokens
        )


class LLMFunctionCall(BaseModel):
    """
    Structure of LLM response indicating it "wants" to call a function.
    Modeled after OpenAI spec for `function_call` field in ChatCompletion API.
    """

    name: str  # name of function to call
    arguments: Optional[Dict[str, Any]] = None

    @staticmethod
    def from_dict(message: Dict[str, Any]) -> "LLMFunctionCall":
        """
        Initialize from dictionary.
        Args:
            d: dictionary containing fields to initialize
        """
        fun_call = LLMFunctionCall(name=message["name"])
        fun_args_str = message["arguments"]
        # sometimes may be malformed with invalid indents,
        # so we try to be safe by removing newlines.
        if fun_args_str is not None:
            fun_args_str = fun_args_str.replace("\n", "").strip()
            dict_or_list = parse_imperfect_json(fun_args_str)

            if not isinstance(dict_or_list, dict):
                raise ValueError(
                    f"""
                        Invalid function args: {fun_args_str}
                        parsed as {dict_or_list},
                        which is not a valid dict.
                        """
                )
            fun_args = dict_or_list
        else:
            fun_args = None
        fun_call.arguments = fun_args

        return fun_call

    def __str__(self) -> str:
        return "FUNC: " + json.dumps(self.dict(), indent=2)


class LLMFunctionSpec(BaseModel):
    """
    Description of a function available for the LLM to use.
    To be used when calling the LLM `chat()` method with the `functions` parameter.
    Modeled after OpenAI spec for `functions` fields in ChatCompletion API.
    """

    name: str
    description: str
    parameters: Dict[str, Any]


class OpenAIToolCall(BaseModel):
    """
    Represents a single tool call in a list of tool calls generated by OpenAI LLM API.
    See https://platform.openai.com/docs/api-reference/chat/create

    Attributes:
        id: The id of the tool call.
        type: The type of the tool call;
            only "function" is currently possible (7/26/24).
        function: The function call.
    """

    id: str | None = None
    type: ToolTypes = "function"
    function: LLMFunctionCall | None = None

    @staticmethod
    def from_dict(message: Dict[str, Any]) -> "OpenAIToolCall":
        """
        Initialize from dictionary.
        Args:
            d: dictionary containing fields to initialize
        """
        id = message["id"]
        type = message["type"]
        function = LLMFunctionCall.from_dict(message["function"])
        return OpenAIToolCall(id=id, type=type, function=function)

    def __str__(self) -> str:
        if self.function is None:
            return ""
        return "OAI-TOOL: " + json.dumps(self.function.dict(), indent=2)


class OpenAIToolSpec(BaseModel):
    type: ToolTypes
    strict: Optional[bool] = None
    function: LLMFunctionSpec


class OpenAIJsonSchemaSpec(BaseModel):
    strict: Optional[bool] = None
    function: LLMFunctionSpec

    def to_dict(self) -> Dict[str, Any]:
        json_schema: Dict[str, Any] = {
            "name": self.function.name,
            "description": self.function.description,
            "schema": self.function.parameters,
        }
        if self.strict is not None:
            json_schema["strict"] = self.strict

        return {
            "type": "json_schema",
            "json_schema": json_schema,
        }


class LLMTokenUsage(BaseModel):
    """
    Usage of tokens by an LLM.
    """

    prompt_tokens: int = 0
    cached_tokens: int = 0
    completion_tokens: int = 0
    cost: float = 0.0
    calls: int = 0  # how many API calls - not used as of 2025-04-04

    def reset(self) -> None:
        self.prompt_tokens = 0
        self.cached_tokens = 0
        self.completion_tokens = 0
        self.cost = 0.0
        self.calls = 0

    def __str__(self) -> str:
        return (
            f"Tokens = "
            f"(prompt {self.prompt_tokens}, cached {self.cached_tokens}, "
            f"completion {self.completion_tokens}), "
            f"Cost={self.cost}, Calls={self.calls}"
        )

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens


class Role(str, Enum):
    """
    Possible roles for a message in a chat.
    """

    USER = "user"
    SYSTEM = "system"
    ASSISTANT = "assistant"
    FUNCTION = "function"
    TOOL = "tool"


class LLMMessage(BaseModel):
    """
    Class representing an entry in the msg-history sent to the LLM API.
    It could be one of these:
    - a user message
    - an LLM ("Assistant") response
    - a fn-call or tool-call-list from an OpenAI-compatible LLM API response
    - a result or results from executing a fn or tool-call(s)
    """

    role: Role
    name: Optional[str] = None
    tool_call_id: Optional[str] = None  # which OpenAI LLM tool this is a response to
    tool_id: str = ""  # used by OpenAIAssistant
    content: str
    files: List[FileAttachment] = []
    function_call: Optional[LLMFunctionCall] = None
    tool_calls: Optional[List[OpenAIToolCall]] = None
    timestamp: datetime = Field(default_factory=datetime.utcnow)
    # link to corresponding chat document, for provenance/rewind purposes
    chat_document_id: str = ""

    def api_dict(self, model: str, has_system_role: bool = True) -> Dict[str, Any]:
        """
        Convert to dictionary for API request, keeping ONLY
        the fields that are expected in an API call!
        E.g., DROP the tool_id, since it is only for use in the Assistant API,
            not the completion API.

        Args:
            has_system_role: whether the message has a system role (if not,
                set to "user" role)
        Returns:
            dict: dictionary representation of LLM message
        """
        d = self.dict()
        files: List[FileAttachment] = d.pop("files")
        if len(files) > 0 and self.role == Role.USER:
            # In there are files, then content is an array of
            # different content-parts
            d["content"] = [
                dict(
                    type="text",
                    text=self.content,
                )
            ] + [f.to_dict(model) for f in self.files]

        # if there is a key k = "role" with value "system", change to "user"
        # in case has_system_role is False
        if not has_system_role and "role" in d and d["role"] == "system":
            d["role"] = "user"
            if "content" in d:
                d["content"] = "[ADDITIONAL SYSTEM MESSAGE:]\n\n" + d["content"]
        # drop None values since API doesn't accept them
        dict_no_none = {k: v for k, v in d.items() if v is not None}
        if "name" in dict_no_none and dict_no_none["name"] == "":
            # OpenAI API does not like empty name
            del dict_no_none["name"]
        if "function_call" in dict_no_none:
            # arguments must be a string
            if "arguments" in dict_no_none["function_call"]:
                dict_no_none["function_call"]["arguments"] = json.dumps(
                    dict_no_none["function_call"]["arguments"]
                )
        if "tool_calls" in dict_no_none:
            # convert tool calls to API format
            for tc in dict_no_none["tool_calls"]:
                if "arguments" in tc["function"]:
                    # arguments must be a string
                    tc["function"]["arguments"] = json.dumps(
                        tc["function"]["arguments"]
                    )
        # IMPORTANT! drop fields that are not expected in API call
        dict_no_none.pop("tool_id", None)
        dict_no_none.pop("timestamp", None)
        dict_no_none.pop("chat_document_id", None)
        return dict_no_none

    def __str__(self) -> str:
        if self.function_call is not None:
            content = "FUNC: " + json.dumps(self.function_call)
        else:
            content = self.content
        name_str = f" ({self.name})" if self.name else ""
        return f"{self.role} {name_str}: {content}"


class LLMResponse(BaseModel):
    """
    Class representing response from LLM.
    """

    message: str
    reasoning: str = ""  # optional reasoning text from reasoning models
    # TODO tool_id needs to generalize to multi-tool calls
    tool_id: str = ""  # used by OpenAIAssistant
    oai_tool_calls: Optional[List[OpenAIToolCall]] = None
    function_call: Optional[LLMFunctionCall] = None
    usage: Optional[LLMTokenUsage] = None
    cached: bool = False

    def __str__(self) -> str:
        if self.function_call is not None:
            return str(self.function_call)
        elif self.oai_tool_calls:
            return "\n".join(str(tc) for tc in self.oai_tool_calls)
        else:
            return self.message

    def to_LLMMessage(self) -> LLMMessage:
        """Convert LLM response to an LLMMessage, to be included in the
        message-list sent to the API.
        This is currently NOT used in any significant way in the library, and is only
        provided as a utility to construct a message list for the API when directly
        working with an LLM object.

        In a `ChatAgent`, an LLM response is first converted to a ChatDocument,
        which is in turn converted to an LLMMessage via `ChatDocument.to_LLMMessage()`
        See `ChatAgent._prep_llm_messages()` and `ChatAgent.llm_response_messages`
        """
        return LLMMessage(
            role=Role.ASSISTANT,
            content=self.message,
            name=None if self.function_call is None else self.function_call.name,
            function_call=self.function_call,
            tool_calls=self.oai_tool_calls,
        )

    def get_recipient_and_message(
        self,
    ) -> Tuple[str, str]:
        """
        If `message` or `function_call` of an LLM response contains an explicit
        recipient name, return this recipient name and `message` stripped
        of the recipient name if specified.

        Two cases:
        (a) `message` contains addressing string "TO: <name> <content>", or
        (b) `message` is empty and function_call/tool_call with explicit `recipient`


        Returns:
            (str): name of recipient, which may be empty string if no recipient
            (str): content of message

        """

        if self.function_call is not None:
            # in this case we ignore message, since all information is in function_call
            msg = ""
            args = self.function_call.arguments
            recipient = ""
            if isinstance(args, dict):
                recipient = args.get("recipient", "")
            return recipient, msg
        else:
            msg = self.message
            if self.oai_tool_calls is not None:
                # get the first tool that has a recipient field, if any
                for tc in self.oai_tool_calls:
                    if tc.function is not None and tc.function.arguments is not None:
                        recipient = tc.function.arguments.get(
                            "recipient"
                        )  # type: ignore
                        if recipient is not None and recipient != "":
                            return recipient, ""

        # It's not a function or tool call, so continue looking to see
        # if a recipient is specified in the message.

        # First check if message contains "TO: <recipient> <content>"
        recipient_name, content = parse_message(msg) if msg is not None else ("", "")
        # check if there is a top level json that specifies 'recipient',
        # and retain the entire message as content.
        if recipient_name == "":
            recipient_name = top_level_json_field(msg, "recipient") if msg else ""
            content = msg
        return recipient_name, content


# Define an abstract base class for language models
class LanguageModel(ABC):
    """
    Abstract base class for language models.
    """

    # usage cost by model, accumulates here
    usage_cost_dict: Dict[str, LLMTokenUsage] = {}

    def __init__(self, config: LLMConfig = LLMConfig()):
        self.config = config
        self.chat_model_orig = config.chat_model

    @staticmethod
    def create(config: Optional[LLMConfig]) -> Optional["LanguageModel"]:
        """
        Create a language model.
        Args:
            config: configuration for language model
        Returns: instance of language model
        """
        if type(config) is LLMConfig:
            raise ValueError(
                """
                Cannot create a Language Model object from LLMConfig.
                Please specify a specific subclass of LLMConfig e.g.,
                OpenAIGPTConfig. If you are creating a ChatAgent from
                a ChatAgentConfig, please specify the `llm` field of this config
                as a specific subclass of LLMConfig, e.g., OpenAIGPTConfig.
                """
            )
        from langroid.language_models.azure_openai import AzureGPT
        from langroid.language_models.mock_lm import MockLM, MockLMConfig
        from langroid.language_models.openai_gpt import OpenAIGPT

        if config is None or config.type is None:
            return None

        if config.type == "mock":
            return MockLM(cast(MockLMConfig, config))

        openai: Union[Type[AzureGPT], Type[OpenAIGPT]]

        if config.type == "azure":
            openai = AzureGPT
        else:
            openai = OpenAIGPT
        cls = dict(
            openai=openai,
        ).get(config.type, openai)
        return cls(config)  # type: ignore

    @staticmethod
    def user_assistant_pairs(lst: List[str]) -> List[Tuple[str, str]]:
        """
        Given an even-length sequence of strings, split into a sequence of pairs

        Args:
            lst (List[str]): sequence of strings

        Returns:
            List[Tuple[str,str]]: sequence of pairs of strings
        """
        evens = lst[::2]
        odds = lst[1::2]
        return list(zip(evens, odds))

    @staticmethod
    def get_chat_history_components(
        messages: List[LLMMessage],
    ) -> Tuple[str, List[Tuple[str, str]], str]:
        """
        From the chat history, extract system prompt, user-assistant turns, and
        final user msg.

        Args:
            messages (List[LLMMessage]): List of messages in the chat history

        Returns:
            Tuple[str, List[Tuple[str,str]], str]:
                system prompt, user-assistant turns, final user msg

        """
        # Handle various degenerate cases
        messages = [m for m in messages]  # copy
        DUMMY_SYS_PROMPT = "You are a helpful assistant."
        DUMMY_USER_PROMPT = "Follow the instructions above."
        if len(messages) == 0 or messages[0].role != Role.SYSTEM:
            logger.warning("No system msg, creating dummy system prompt")
            messages.insert(0, LLMMessage(content=DUMMY_SYS_PROMPT, role=Role.SYSTEM))
        system_prompt = messages[0].content

        # now we have messages = [Sys,...]
        if len(messages) == 1:
            logger.warning(
                "Got only system message in chat history, creating dummy user prompt"
            )
            messages.append(LLMMessage(content=DUMMY_USER_PROMPT, role=Role.USER))

        # now we have messages = [Sys, msg, ...]

        if messages[1].role != Role.USER:
            messages.insert(1, LLMMessage(content=DUMMY_USER_PROMPT, role=Role.USER))

        # now we have messages = [Sys, user, ...]
        if messages[-1].role != Role.USER:
            logger.warning(
                "Last message in chat history is not a user message,"
                " creating dummy user prompt"
            )
            messages.append(LLMMessage(content=DUMMY_USER_PROMPT, role=Role.USER))

        # now we have messages = [Sys, user, ..., user]
        # so we omit the first and last elements and make pairs of user-asst messages
        conversation = [m.content for m in messages[1:-1]]
        user_prompt = messages[-1].content
        pairs = LanguageModel.user_assistant_pairs(conversation)
        return system_prompt, pairs, user_prompt

    @abstractmethod
    def set_stream(self, stream: bool) -> bool:
        """Enable or disable streaming output from API.
        Return previous value of stream."""
        pass

    @abstractmethod
    def get_stream(self) -> bool:
        """Get streaming status"""
        pass

    @abstractmethod
    def generate(self, prompt: str, max_tokens: int = 200) -> LLMResponse:
        pass

    @abstractmethod
    async def agenerate(self, prompt: str, max_tokens: int = 200) -> LLMResponse:
        pass

    @abstractmethod
    def chat(
        self,
        messages: Union[str, List[LLMMessage]],
        max_tokens: int = 200,
        tools: Optional[List[OpenAIToolSpec]] = None,
        tool_choice: ToolChoiceTypes | Dict[str, str | Dict[str, str]] = "auto",
        functions: Optional[List[LLMFunctionSpec]] = None,
        function_call: str | Dict[str, str] = "auto",
        response_format: Optional[OpenAIJsonSchemaSpec] = None,
    ) -> LLMResponse:
        """
        Get chat-completion response from LLM.

        Args:
            messages: message-history to send to the LLM
            max_tokens: max tokens to generate
            tools: tools available for the LLM to use in its response
            tool_choice: tool call mode, one of "none", "auto", "required",
                or a dict specifying a specific tool.
            functions: functions available for LLM to call (deprecated)
            function_call: function calling mode, "auto", "none", or a specific fn
                    (deprecated)
        """

        pass

    @abstractmethod
    async def achat(
        self,
        messages: Union[str, List[LLMMessage]],
        max_tokens: int = 200,
        tools: Optional[List[OpenAIToolSpec]] = None,
        tool_choice: ToolChoiceTypes | Dict[str, str | Dict[str, str]] = "auto",
        functions: Optional[List[LLMFunctionSpec]] = None,
        function_call: str | Dict[str, str] = "auto",
        response_format: Optional[OpenAIJsonSchemaSpec] = None,
    ) -> LLMResponse:
        """Async version of `chat`. See `chat` for details."""
        pass

    def __call__(self, prompt: str, max_tokens: int) -> LLMResponse:
        return self.generate(prompt, max_tokens)

    @staticmethod
    def _fallback_model_names(model: str) -> List[str]:
        parts = model.split("/")
        fallbacks = []
        for i in range(1, len(parts)):
            fallbacks.append("/".join(parts[i:]))
        return fallbacks

    def info(self) -> ModelInfo:
        """Info of relevant chat model"""
        orig_model = (
            self.config.completion_model
            if self.config.use_completion_for_chat
            else self.chat_model_orig
        )
        return get_model_info(orig_model, self._fallback_model_names(orig_model))

    def completion_info(self) -> ModelInfo:
        """Info of relevant completion model"""
        orig_model = (
            self.chat_model_orig
            if self.config.use_chat_for_completion
            else self.config.completion_model
        )
        return get_model_info(orig_model, self._fallback_model_names(orig_model))

    def supports_functions_or_tools(self) -> bool:
        """
        Does this Model's API support "native" tool-calling, i.e.
        can we call the API with arguments that contain a list of available tools,
        and their schemas?
        Note that, given the plethora of LLM provider APIs this determination is
        imperfect at best, and leans towards returning True.
        When the API calls fails with an error indicating tools are not supported,
        then users are encouraged to use the Langroid-based prompt-based
        ToolMessage mechanism, which works with ANY LLM. To enable this,
        in your ChatAgentConfig, set `use_functions_api=False`, and `use_tools=True`.
        """
        return self.info().has_tools

    def chat_context_length(self) -> int:
        return self.config.chat_context_length or DEFAULT_CONTEXT_LENGTH

    def completion_context_length(self) -> int:
        return self.config.completion_context_length or DEFAULT_CONTEXT_LENGTH

    def chat_cost(self) -> Tuple[float, float, float]:
        """
        Return the cost per 1000 tokens for chat completions.

        Returns:
            Tuple[float, float, float]: (input_cost, cached_cost, output_cost)
                per 1000 tokens
        """
        return (0.0, 0.0, 0.0)

    def reset_usage_cost(self) -> None:
        for mdl in [self.config.chat_model, self.config.completion_model]:
            if mdl is None:
                return
            if mdl not in self.usage_cost_dict:
                self.usage_cost_dict[mdl] = LLMTokenUsage()
            counter = self.usage_cost_dict[mdl]
            counter.reset()

    def update_usage_cost(
        self, chat: bool, prompts: int, completions: int, cost: float
    ) -> None:
        """
        Update usage cost for this LLM.
        Args:
            chat (bool): whether to update for chat or completion model
            prompts (int): number of tokens used for prompts
            completions (int): number of tokens used for completions
            cost (float): total token cost in USD
        """
        mdl = self.config.chat_model if chat else self.config.completion_model
        if mdl is None:
            return
        if mdl not in self.usage_cost_dict:
            self.usage_cost_dict[mdl] = LLMTokenUsage()
        counter = self.usage_cost_dict[mdl]
        counter.prompt_tokens += prompts
        counter.completion_tokens += completions
        counter.cost += cost
        counter.calls += 1

    @classmethod
    def usage_cost_summary(cls) -> str:
        s = ""
        for model, counter in cls.usage_cost_dict.items():
            s += f"{model}: {counter}\n"
        return s

    @classmethod
    def tot_tokens_cost(cls) -> Tuple[int, float]:
        """
        Return total tokens used and total cost across all models.
        """
        total_tokens = 0
        total_cost = 0.0
        for counter in cls.usage_cost_dict.values():
            total_tokens += counter.total_tokens
            total_cost += counter.cost
        return total_tokens, total_cost

    def get_reasoning_final(self, message: str) -> Tuple[str, str]:
        """Extract "reasoning" and "final answer" from an LLM response, if the
        reasoning is found within configured delimiters, like <think>, </think>.
        E.g.,
        '<think> Okay, let's see, the user wants... </think> 2 + 3 = 5'

        Args:
            message (str): message from LLM

        Returns:
            Tuple[str, str]: reasoning, final answer
        """
        start, end = self.config.thought_delimiters
        if start in message and end in message:
            parts = message.split(start)
            if len(parts) > 1:
                reasoning, final = parts[1].split(end)
                return reasoning, final
        return "", message

    def followup_to_standalone(
        self, chat_history: List[Tuple[str, str]], question: str
    ) -> str:
        """
        Given a chat history and a question, convert it to a standalone question.
        Args:
            chat_history: list of tuples of (question, answer)
            query: follow-up question

        Returns: standalone version of the question
        """
        history = collate_chat_history(chat_history)

        prompt = f"""
        You are an expert at understanding a CHAT HISTORY between an AI Assistant
        and a User, and you are highly skilled in rephrasing the User's FOLLOW-UP
        QUESTION/REQUEST as a STANDALONE QUESTION/REQUEST that can be understood
        WITHOUT the context of the chat history.

        Below is the CHAT HISTORY. When the User asks you to rephrase a
        FOLLOW-UP QUESTION/REQUEST, your ONLY task is to simply return the
        question REPHRASED as a STANDALONE QUESTION/REQUEST, without any additional
        text or context.

        <CHAT_HISTORY>
        {history}
        </CHAT_HISTORY>
        """.strip()

        follow_up_question = f"""
        Please rephrase this as a stand-alone question or request:
        <FOLLOW-UP-QUESTION-OR-REQUEST>
        {question}
        </FOLLOW-UP-QUESTION-OR-REQUEST>
        """.strip()

        show_if_debug(prompt, "FOLLOWUP->STANDALONE-PROMPT= ")
        standalone = self.chat(
            messages=[
                LLMMessage(role=Role.SYSTEM, content=prompt),
                LLMMessage(role=Role.USER, content=follow_up_question),
            ],
            max_tokens=1024,
        ).message.strip()

        show_if_debug(prompt, "FOLLOWUP->STANDALONE-RESPONSE= ")
        return standalone


class StreamingIfAllowed:
    """Context to temporarily enable or disable streaming, if allowed globally via
    `settings.stream`"""

    def __init__(self, llm: LanguageModel, stream: bool = True):
        self.llm = llm
        self.stream = stream

    def __enter__(self) -> None:
        self.old_stream = self.llm.set_stream(settings.stream and self.stream)

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        self.llm.set_stream(self.old_stream)
