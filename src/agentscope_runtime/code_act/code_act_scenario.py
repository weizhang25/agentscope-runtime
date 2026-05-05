# -*- coding: utf-8 -*-

import logging
import time
import uvicorn
import asyncio
from threading import Thread
import requests
from pydantic import BaseModel
from fastapi import FastAPI
from typing import Any, Tuple, Dict

from agentscope_runtime.sandbox.box.sandbox import SandboxAsync
from agentscope_runtime.engine.services.sandbox.sandbox_service import SandboxService

from mcp.types import CallToolResult, TextContent

logger = logging.getLogger(__name__)


class CodeActRunToolServer():
    """An http server, wraps a sandbox, which hosts an mcp server, which hosts multiple tools."""

    def __init__(self, host: str, port: int, **kwargs):
        """TODO: caller should send in all tool definitions in kwargs. """
        if not host or not port:
            raise ValueError('CodeActRunToolServer missing host or port argument')

        self.host = host
        self.port = port
        self.kwargs = kwargs or None
        
    
    async def start(self):
        """Launch the server."""
        await self._create_code_act_run_tool_sandbox()        
        logger.info('code act run tool sandbox is ready')

        class RunToolRequest(BaseModel):
            tool_name: str
            tool_args: dict

        app = FastAPI(
            title="AgentScope Runtime CodeAct Run Tool Server",
            version="1.0",
            description="Agentscope runtime codeAct run tool server",
        )

        @app.post("/run_tool")
        async def run_tool(request: RunToolRequest) -> CallToolResult:
            """Request to run a tool that is on this server."""
            logger.info(f'/run_tool, tool_name:|{request.tool_name}|, tool_args:|{request.tool_args}|')
            try:
                if self.is_async_sandbox:
                    result = await self.sandbox.call_tool_async(request.tool_name, request.tool_args)
                else:
                    result = self.sandbox.call_tool(request.tool_name, request.tool_args)

                logger.info(f'/run_tool, result, type:{type(result)}, value:|{result}|')
                return result
            except Exception as e:
                error_content = TextContent()
                error_content.text = f'failed to handle run tool request {request} on {self.sandbox.sandbox_id}: {str(e)}'
                error_content.description = 'sandbox call tool error'
                contents = [error_content]
                error_result = CallToolResult(content=contents, is_error=True)
                return error_result

        @app.get('/list_tools')
        async def list_tools() -> Dict[str, Dict[str, Dict[str, Any]]]:
            """Return the information of the tools that are hosted on this server.
            {server_name: {tool_name: {name: 'foo', json_schema: {...} } } }"""
            try:
                if self.is_async_sandbox:
                    result = await self.sandbox.list_tools_async()
                else:
                    result = self.sandbox.list_tools()

                logger.info(f'/list_tools, result, type:{type(result)}, value:|{result}|')
                return result
            except Exception as e:
                return {'error_message': f'failed to handle list tools request on {self.sandbox.sandbox_id}: {str(e)}'}

        self.code_act_server_thread = Thread(target=uvicorn.run, args=[app], kwargs={'host': self.host, 'port': self.port, 'access_log': True}, daemon=True)
        logger.info(f'run_code_act_run_tool_server, before code_act_server_thread start')
        self.code_act_server_thread.start()
        time.sleep(3)
        logger.info(f"CodeActRunToolServer http server is running on {self.host}:{self.port}")

    async def _create_code_act_run_tool_sandbox(self):
        """Create a sandbox that hosts an mcp server, which hosts all tools agent needs.
        TODO: just pass in such a sandbox service in self.kwargs."""
        # Create and start the sandbox service
        self.sandbox_service = SandboxService()
        await self.sandbox_service.start()

        session_id = "session_123"
        user_id = "user_12345"

        # Connect to the sandbox, specifying the required sandbox type
        sandboxes = self.sandbox_service.connect(
            session_id=session_id,
            user_id=user_id,
            sandbox_types=["base"],
        )

        self.sandbox = sandboxes[0]

        # register CodeActTool to mcp
        # from kwargs
        mcp_server_configs = {
            "mcpServers": {
                "time": {
                    "command": "uvx",
                    "args": [
                        "mcp-server-time",
                        "--local-timezone=America/New_York",
                    ],
                },
            },
        }

        # Add MCP server to the sandbox
        self.sandbox.add_mcp_servers(server_configs=mcp_server_configs)

        self.is_async_sandbox = isinstance(self.sandbox, SandboxAsync)
        logger.info(f'CodeActRunToolServer is_async_sandbox:{self.is_async_sandbox}')

        # List all available tools (now includes MCP tools)
        all_tools = (await self.sandbox.list_tools_async() if self.is_async_sandbox else self.sandbox.list_tools())
        print(f'CodeActRunToolServer all tools in sandbox: {all_tools}')


    async def stop(self):
        logger.info('stopping CodeActRunToolServer')
        try:
            await self.sandbox_service.stop()
            logger.info('CodeActRunToolServer is stopped')
        except Exception as e:
            logger.error(f'failed to properly stop CodeActRunToolServer', exc_info=True)


class PseudoAgent:
    """A fake agent that includes codeActTool and codeActServer."""
    async def setup(self): 
        self.code_act_server_host = '0.0.0.0'
        self.code_act_server_port = 12345
      
        self.code_act_server = CodeActRunToolServer(host = self.code_act_server_host, port = self.code_act_server_port)
        await self.code_act_server.start()

        #TODO: start codeActTool in a sandbox

    
    async def simulate_code_act_execution(self):
        """Simulate the behavior of the sandbox where run code tool is hosted.
        Agent sends llm-generated code to code act tool. Code act tool calls Code act server to make actual tool calls."""

        # TODO: replace these naked calls with codeActTool sandbox call_tool calls.
        payload = {'tool_name': 'run_ipython_cell', 'tool_args': {'code': 'print("----line 1---")\nprint("----line 2---")'}}
        response = requests.post(f"http://{self.code_act_server_host}:{self.code_act_server_port}/run_tool", json=payload)
        logger.info(f'---call code act run tool server result, type:{type(response)}')
        logger.info(f'---call code act run tool server result, value:{response}')


        payload = {'tool_name': 'run_ipython_cell', 'tool_args': {'code': 'def foo():\n  return 1\n\nfoo()'}}
        response = requests.post(f"http://{self.code_act_server_host}:{self.code_act_server_port}/run_tool", json=payload)
        logger.info(f'---call code act run tool server result, type:{type(response)}')
        logger.info(f'---call code act run tool server result, value:{response}')

        response = requests.get(f"http://{self.code_act_server_host}:{self.code_act_server_port}/list_tools")
        logger.info(f'---call code act run tool server list tools result, type:{type(response)}')
        logger.info(f'---call code act run tool server list tools result, value:{response.json()}')
       
    
    async def shutdown(self):
        self.code_act_server.stop()
        # TODO: shutdown codeActTool sandbox


async def main():
    agent = PseudoAgent()
    await agent.setup()
    await agent.simulate_code_act_execution()
    await agent.shutdown()


if __name__ == '__main__':
    asyncio.run(main())
