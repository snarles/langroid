import pytest

from langroid.agent.chat_agent import ChatAgent, ChatAgentConfig
from langroid.agent.task import Task, TaskConfig
from langroid.agent.tool_message import ToolMessage
from langroid.agent.tools.orchestration import DoneTool
from langroid.agent.tools.task_tool import TaskTool
from langroid.language_models.mock_lm import MockLMConfig
from langroid.language_models.openai_gpt import OpenAIGPTConfig


class MultiplierTool(ToolMessage):
    """A simple calculator tool for testing."""

    request: str = "multiplier_tool"
    purpose: str = "To calculate the product of two numbers."
    a: int
    b: int

    def handle(self) -> str:
        return self.a * self.b


def test_task_tool_mock_main_agent():
    """
    Test that when MockAgent uses TaskTool, it  properly spawns a sub-agent
    that can use tools and complete tasks.
    """

    # Configure the main agent to use TaskTool:
    # The MockLM has a fixed response, which is the TaskTool request
    main_config = ChatAgentConfig(
        llm=MockLMConfig(
            default_response=TaskTool(
                system_message=f"""
                    You are a calculator assistant. When asked to 
                    calculate, use the TOOL `{MultiplierTool.name()}` to multiply the 
                    numbers, then use the TOOL `{DoneTool.name()}` to return the result
                    """,
                prompt="Multiply 5 and 7",
                model="gpt-4.1-mini",
                tools=["multiplier_tool"],
                max_iterations=5,
            ).json()
        ),
        name="MainAgent",
    )
    main_agent = ChatAgent(main_config)

    # Enable TaskTool and MultiplierTool for the main agent.
    # The MultiplierTool must be enabled for the main agent,
    # since the TaskTool handler will create a sub-agent that uses it,
    # and the handler only has access to tools enabled for the main agent.
    main_agent.enable_message([TaskTool, MultiplierTool], use=True, handle=True)

    # Create main task that stops after handling the task_tool
    task = Task(
        main_agent,
        name="MainTask",
        interactive=False,
        config=TaskConfig(
            done_sequences=["T,A"],  # LLM (Tool (TaskTool)), Agent(Handled) -> done
        ),
    )

    # Run the task
    result = task.run(msg="Please calculate something")

    # The result should be from the sub-agent's execution
    assert result is not None, "Task should return a result"
    assert "35" in result.content, "Result should contain the multiplication result"


class NebrowskiTool(ToolMessage):
    """A tool that computes the exotic Nebrowski operation."""

    request: str = "nebrowski_tool"
    purpose: str = """
        To compute the Nebrowski operation of two numbers: 
        neb(a,b) = 3a + b
    """
    a: int
    b: int

    def handle(self) -> str:
        # The Nebrowski operation: 3a + b
        result = 3 * self.a + self.b
        return f"Nebrowski({self.a}, {self.b}) = {result}"


def _create_nebrowski_task():
    """
    Helper function to create a Nebrowski task for both sync and async tests.
    Returns a configured Task ready to run.
    """
    # Configure the main agent with a real LLM
    main_config = ChatAgentConfig(
        llm=OpenAIGPTConfig(),  # Uses default model
        handle_llm_no_tool="you forgot to use one of your TOOLs!",
        system_message=f"""
        You are a Nebrowski operation specialist. The Nebrowski operation is an exotic 
        mathematical function that takes two numbers and produces a result.
        BUT you do NOT know how to compute it yourself!
        
        When the user asks you to compute nested Nebrowski operations like 
        Nebrowski(a, Nebrowski(b, c)), you MUST:
        
        1. Break it down into individual Nebrowski operations
        2. Use the TOOL `{TaskTool.name()}` to delegate each Nebrowski 
            operation to a sub-agent
        3. The sub-agent knows how to use the `{NebrowskiTool.name()}` tool
        
        For example, to compute Nebrowski(10, Nebrowski(3, 2)):
        - First compute inner: Nebrowski(3, 2) = result1 (using TaskTool)
        - Then compute outer: Nebrowski(10, result1) (using TaskTool)
        - Return the final result
        
        IMPORTANT: You must use TaskTool for EACH Nebrowski operation.
        Configure the TaskTool with:
        - system_message: Instructions for the sub-agent to compute Nebrowski
        - prompt: The specific Nebrowski task (e.g., "Compute Nebrowski(3, 2)")
        - tools: ["nebrowski_tool"]
        - model: "gpt-4o-mini"
        
        Remember: You cannot compute Nebrowski operations yourself - you must 
        delegate to sub-agents!
        
        You MUST use the TOOL `{DoneTool.name()}` to return the final result!
        """,
        name="NebrowskiAgent",
    )
    main_agent = ChatAgent(main_config)

    # Enable TaskTool and NebrowskiTool
    main_agent.enable_message(
        [DoneTool, TaskTool, NebrowskiTool], use=True, handle=True
    )

    # Create task with appropriate configuration
    task = Task(
        main_agent,
        name="NebrowskiTask",
        interactive=False,
    )

    return task


def test_task_tool_real_llm_nebrowski():
    """
    Test that a real LLM agent can compute nested Nebrowski operations
    by using TaskTool to delegate each Nebrowski computation to sub-agents.
    """
    task = _create_nebrowski_task()

    # Run the task - compute Nebrowski(10, Nebrowski(3, 2))
    # Expected: Nebrowski(3, 2) = 11, then Nebrowski(10, 11) = 41
    result = task.run("Compute Nebrowski(10, Nebrowski(3, 2))", turns=15)

    # Verify the result
    assert result is not None, "Task should return a result"
    assert "41" in result.content, "Result should contain the final Nebrowski result"


@pytest.mark.asyncio
async def test_task_tool_real_llm_nebrowski_async():
    """
    Async version: Test that a real LLM agent can compute nested Nebrowski operations
    by using TaskTool to delegate each Nebrowski computation to sub-agents.
    """
    task = _create_nebrowski_task()

    # Run the task asynchronously - compute Nebrowski(10, Nebrowski(3, 2))
    # Expected: Nebrowski(3, 2) = 11, then Nebrowski(10, 11) = 41
    result = await task.run_async("Compute Nebrowski(10, Nebrowski(3, 2))", turns=15)

    # Verify the result
    assert result is not None, "Task should return a result"
    assert "41" in result.content, "Result should contain the final Nebrowski result"


def test_task_tool_all_tools():
    """
    Test that tools="all" enables all available tools for the sub-agent.
    """
    # Create a main agent with multiple tools available
    main_config = ChatAgentConfig(
        llm=MockLMConfig(
            default_response=TaskTool(
                agent_name="Calculator",
                system_message=f"""
                    You are a multi-tool assistant. Use the appropriate tool
                    to complete the task, then use `{DoneTool.name()}` to return the 
                    result.
                    """,
                prompt="""
                    Multiply 4 and 6, call it x, then compute Nebrowski(x, 5)
                    """,
                model="gpt-4o-mini",
                tools=["ALL"],  # Enable all tools
                max_iterations=20,
            ).json()
        ),
        name="MainAgent",
    )
    main_agent = ChatAgent(main_config)

    # Set up multiple tools for the main agent
    main_agent.enable_message(
        [TaskTool, MultiplierTool, NebrowskiTool], use=True, handle=True
    )

    # Create task
    task = Task(
        main_agent,
        name="AllToolsTask",
        interactive=False,
        config=TaskConfig(
            done_sequences=["T,A"],  # LLM (Tool), Agent(Handled) -> done
        ),
    )

    # Run the task: input text is immaterial since the
    # MockLM is hard-coded to return the TaskTool request
    result = task.run(msg="Test all tools")

    # Verify that the sub-agent had access to all tools
    # Expected: Multiply 4 and 6 = 24, Nebrowski(3, 5) = 14
    assert result is not None, "Task should return a result"
    assert "77" in result.content, "Result should contain 77"

    # Verify that parent chain is maintained through TaskTool
    # When TaskTool creates a prompt ChatDocument with parent_id pointing to the
    # TaskTool message, and passes it to the subtask, the subtask's init() method
    # should preserve that parent_id even though it deep copies the message.
    # This ensures the parent chain is not broken.
    assert hasattr(result, "parent"), "Result should have a parent pointer"

    # Traverse up the parent chain to find the TaskTool message
    current = result
    task_tool_found = False
    depth = 0
    # Prevent infinite loops, and allow enough look-back
    # to accommodate tool-forgetting retries that may occur.
    max_depth = 40

    while current and depth < max_depth:
        # Check if current message is from TaskTool
        if current.content and "task_tool" in current.content.lower():
            task_tool_found = True
            break

        # Also check if it's a tool message with TaskTool request
        try:
            tool_messages = main_agent.try_get_tool_messages(current.content)
            if tool_messages:
                for tool_msg in tool_messages:
                    if isinstance(tool_msg, TaskTool):
                        task_tool_found = True
                        break
        except Exception:
            pass  # Not a tool message

        if task_tool_found:
            break
        current = current.parent
        depth += 1

    assert task_tool_found, "Parent chain should lead back to TaskTool message"


def test_task_tool_none_tools():
    """
    Test that tools="none" disables all tools except DoneTool for the sub-agent.
    """
    # Create a main agent that delegates with no tools
    main_config = ChatAgentConfig(
        llm=MockLMConfig(
            default_response=TaskTool(
                agent_name="Calculator",
                system_message=f"""
                    You are an assistant with no tools. Just respond directly
                    to the prompt and use `{DoneTool.name()}` to return your answer.
                    """,
                prompt="What is 2 + 2? Just tell me the answer.",
                model="gpt-4o-mini",
                tools=["NONE"],  # Disable all tools except DoneTool
                max_iterations=20,
            ).json()
        ),
        name="MainAgent",
    )
    main_agent = ChatAgent(main_config)

    # Enable TaskTool and other tools for the main agent
    # (sub-agent won't have access to these)
    main_agent.enable_message(
        [TaskTool, MultiplierTool, NebrowskiTool], use=True, handle=True
    )

    # Create task
    task = Task(
        main_agent,
        name="NoToolsTask",
        interactive=False,
        config=TaskConfig(
            done_sequences=["T,A"],  # LLM (Tool), Agent(Handled) -> done
        ),
    )

    # Run the task
    result = task.run(msg="Test no tools")

    # Verify that the task completed (sub-agent can still use DoneTool)
    assert result is not None, "Task should return a result"
