# -*- coding: utf-8 -*-

import logging
import time
import uvicorn
import asyncio
from threading import Thread
import requests
from pydantic import BaseModel
from fastapi import FastAPI, HTTPException
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
        self.tool_return_types = {}
        
    
    async def start(self):
        """Launch the server."""

        # self.sandbox, self.sandbox_service
        await self._create_code_act_run_tool_sandbox()        
        logger.info('code act run tool sandbox is ready')
        # self.tool_return_types
        await self._set_tool_return_types()

        class RunToolRequest(BaseModel):
            tool_name: str
            tool_args: dict

        app = FastAPI(
            title="AgentScope Runtime CodeAct Run Tool Server",
            version="1.0",
            description="Agentscope runtime codeAct run tool server",
        )

        async def _parse_tool_return_value(tool_name: str, call_tool_result: dict) -> Any:
            """parse the actual tool return value from a CallToolResult object."""
            try:
                contents = call_tool_result.get('content', [])
                tool_result = contents[0]['text'] if contents and contents[0].get('text', '') else ''

                if bool(call_tool_result.get('isError', False)):
                    error_msg = tool_result if tool_result else 'error cause unknown'
                    raise ValueError(f'tool call {tool_name} failed, {error_msg}')
            
                return_type = self.tool_return_types.get(tool_name, None)
                if return_type:
                    logger.info(f'casting {tool_name} tool result |{tool_result}| of type {type(tool_result)}, to type {return_type}')
                    try:
                        tool_result_new = return_type(tool_result)
                        logger.info(f'casted {tool_name} tool result |{tool_result}| of type {type(tool_result)}, to |{tool_result_new}| of type {return_type}')
                        return tool_result_new
                    except Exception:
                        logger.error(f'failed to cast tool result |{tool_result}| of type {type(tool_result)}, to type {return_type}', exc_info=True)

                logger.info(f'no type cast for {tool_name} tool result |{tool_result}| of type {type(tool_result)}')
                return tool_result
            except Exception as e:
                msg = f'failed to parse tool return value, tool {tool_name}, call_tool_result {call_tool_result}, {str(e)}'
                logger.error(msg)
                raise ValueError(msg)

        @app.post("/run_tool")
        async def run_tool(request: RunToolRequest) -> Any:
            """Request to run a tool that is on this server."""
            logger.info(f'/run_tool, tool_name:|{request.tool_name}|, tool_args:|{request.tool_args}|')
            try:
                # result: a dict from pydantic model obj model_dump()
                if self.is_async_sandbox:
                    result = await self.sandbox.call_tool_async(request.tool_name, request.tool_args)
                else:
                    result = self.sandbox.call_tool(request.tool_name, request.tool_args)

                logger.info(f'/run_tool, result, type:{type(result)}, value:|{result}|')
                result = await _parse_tool_return_value(request.tool_name, result)
                return {'result': result}
            except Exception as e:
                # error_content = TextContent()
                # error_content.text = f'failed to handle run tool request {request} on {self.sandbox.sandbox_id}: {str(e)}'
                # error_content.description = 'sandbox call tool error'
                # contents = [error_content]
                # error_result = CallToolResult(content=contents, is_error=True)
                # return error_result
                raise HTTPException(
                    status_code=500,
                    detail=f"{request.tool_name}, Tool execution failed, {str(e)}")

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



    async def _set_tool_return_types(self):
        """extract and store return types for all tools."""
        logger.info('---------------- setting all tools return types ---------------')
        try:
            # List all available tools (now includes MCP tools)
            all_tools = (await self.sandbox.list_tools_async() if self.is_async_sandbox else self.sandbox.list_tools())
            print(f'CodeActRunToolServer all tools in sandbox: {all_tools}')

            # find tool's return type
            for _, tools_dict in all_tools.items():
                for tool_name, tool_dict in tools_dict.items():
                    # 'foo': {
                    #     'name': 'foo',
                    #     'json_schema': {
                    #         ...,
                    #         'function': {
                    #             ...,
                    #             'output_schema': {
                    #                 'type': 'object',
                    #                 'properties': {
                    #                     "result": {"type": "integer"}
                    #                 }
                    #             }
                    #         }
                    #     }
                    # }
                    output_schema = tool_dict.get('json_schema', {}).get('function', {}).get('output_schema', {})
                    return_type = output_schema.get('properties', {}).get('result', {}).get('type', '')
                    return_type = return_type.lower()
                    if not return_type:
                        self.tool_return_types[tool_name] = type('')
                    elif return_type in ['str', 'string']:
                        self.tool_return_types[tool_name] = type('')
                    elif return_type in ['int', 'integer']:
                        self.tool_return_types[tool_name] = type(1)
                    elif return_type in ['float', 'number', 'double']:
                        self.tool_return_types[tool_name] = type(1.1)
                    elif return_type in ['bool', 'boolean']:
                        self.tool_return_types[tool_name] = type(True)
                    elif return_type in ['array', 'list']:
                        self.tool_return_types[tool_name] = type([])
                    elif return_type in ['dict', 'map', 'dictionary']:
                        self.tool_return_types[tool_name] = type({})
                    elif return_type in ['tuple']:
                        self.tool_return_types[tool_name] = type(())
                    elif return_type in ['set']:
                        self.tool_return_types[tool_name] = type(set())
                    elif return_type in ['bytes']:
                        self.tool_return_types[tool_name] = type(b'')
                    else:
                        self.tool_return_types[tool_name] = type('')
            self.tool_return_types['get_current_time'] = type({})
            logger.info(f'set tool return types to: {self.tool_return_types}')
        except Exception as e:
            msg = f'failed to set tool return types list_tools, {str(e)}'
            logger.error(msg)
            raise ValueError(msg)

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
        # response = requests.get(f"http://{self.code_act_server_host}:{self.code_act_server_port}/list_tools")
        # logger.info(f'---call code act run tool server list tools result, type:{type(response)}')
        # logger.info(f'---call code act run tool server list tools result, value:{response.json()}')

        # payload = {'tool_name': 'run_ipython_cell', 'tool_args': {'code': 'def foo_str():\n  print("foo_str_print")\n  return "asdf"\n\nfoo_str()'}}
        # response = requests.post(f"http://{self.code_act_server_host}:{self.code_act_server_port}/run_tool", json=payload)
        # # logger.info(f'---call code act run tool server result, type:{type(response)}')
        # logger.info(f'---CallToolResult, str func, value:{response.json()}')

        # payload = {'tool_name': 'run_ipython_cell', 'tool_args': {'code': 'def foo_int():\n  print("foo_int_print")\n  return 1\n\nfoo_int()'}}
        # response = requests.post(f"http://{self.code_act_server_host}:{self.code_act_server_port}/run_tool", json=payload)        
        # logger.info(f'---CallToolResult, int func, value:{response.json()}')

        # payload = {'tool_name': 'run_ipython_cell', 'tool_args': {'code': 'def foo_tuple_int_str():\n  print("foo_tuple_print")\n  return 1, "dddd"\n\nfoo_tuple_int_str()'}}
        # response = requests.post(f"http://{self.code_act_server_host}:{self.code_act_server_port}/run_tool", json=payload)        
        # logger.info(f'---CallToolResult, tuple 1 [int, str] func, value:{response.json()}')

        # payload = {'tool_name': 'run_ipython_cell', 'tool_args': {'code': 'def foo_tuple_2():\n  print("foo_tuple_2_print")\n  return (2, "eee")\n\nfoo_tuple_2()'}}
        # response = requests.post(f"http://{self.code_act_server_host}:{self.code_act_server_port}/run_tool", json=payload)        
        # logger.info(f'---CallToolResult, tuple 2 [int, str] func, value:{response.json()}')

        # payload = {'tool_name': 'run_ipython_cell', 'tool_args': {'code': 'def foo_list():\n  print("foo_list_print")\n  return ["a", "b"]\n\nfoo_list()'}}
        # response = requests.post(f"http://{self.code_act_server_host}:{self.code_act_server_port}/run_tool", json=payload)        
        # logger.info(f'---CallToolResult, list func, value:{response.json()}')

        # payload = {'tool_name': 'run_ipython_cell', 'tool_args': {'code': 'def foo_dict():\n  print("foo_dict_print")\n  return {"a": "b", "c":1}\n\nfoo_dict()'}}
        # response = requests.post(f"http://{self.code_act_server_host}:{self.code_act_server_port}/run_tool", json=payload)        
        # logger.info(f'---CallToolResult, dict func, value:{response.json()}')

        # payload = {'tool_name': 'run_ipython_cell', 'tool_args': {'code': 'import datetime as dt\ndef foo_obj():\n  print("foo_obj_print")\n  return dt.timedelta(days=50)\n\nfoo_obj()'}}
        # response = requests.post(f"http://{self.code_act_server_host}:{self.code_act_server_port}/run_tool", json=payload)        
        # logger.info(f'---CallToolResult, obj func, value:{response.json()}')

        # payload = {'tool_name': 'run_ipython_cell', 'tool_args': {'code': 'import datetime as dt\ndef foo_obj_2():\n  print("foo_obj_2_print")\n  a = dt.timedelta(days=50)\n  return a\n\nfoo_obj_2()'}}
        # response = requests.post(f"http://{self.code_act_server_host}:{self.code_act_server_port}/run_tool", json=payload)        
        # logger.info(f'---CallToolResult, obj 2 func, value:{response.json()}')

        # payload = {'tool_name': 'run_ipython_cell', 'tool_args': {'code': 'class XX:\n  def __init__(self, x):\n    self.x = x\n\ndef foo_obj_3():\n  print("foo_obj_3_print")\n  a = XX(3)\n  return a\n\nfoo_obj_3()'}}
        # response = requests.post(f"http://{self.code_act_server_host}:{self.code_act_server_port}/run_tool", json=payload)        
        # logger.info(f'---CallToolResult, obj 3 func, value:{response.json()}')

        # payload = {'tool_name': 'run_ipython_cell', 'tool_args': {'code': 'def foo_abs_no_return():\n  pass\n\nfoo_abs_no_return()'}}
        # response = requests.post(f"http://{self.code_act_server_host}:{self.code_act_server_port}/run_tool", json=payload)        
        # logger.info(f'---CallToolResult, abs no return func, value:{response.json()}')

        payload = {'tool_name': 'run_ipython_cell', 'tool_args': {'code': 'def foo_no_return():\n  print("foo_no_return_print")\n\nfoo_no_return()'}}
        response = requests.post(f"http://{self.code_act_server_host}:{self.code_act_server_port}/run_tool", json=payload)        
        logger.info(f'---CallToolResult, no return func, text value:|{response.text}|, json value:|{response.json()}|')

        payload = {'tool_name': 'get_current_time', 'tool_args': {'timezone': 'America/Los_Angeles'}}
        response = requests.post(f"http://{self.code_act_server_host}:{self.code_act_server_port}/run_tool", json=payload)        
        logger.info(f'---CallToolResult, get_current_time func, text value:|{response.text}|, json value:|{response.json()}|')

       
    
    async def shutdown(self):
        await self.code_act_server.stop()


async def main():
    agent = PseudoAgent()
    await agent.setup()
    await agent.simulate_code_act_execution()
    await agent.shutdown()


if __name__ == '__main__':
    asyncio.run(main())
