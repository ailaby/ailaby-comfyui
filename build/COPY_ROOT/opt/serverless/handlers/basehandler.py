import json
import requests
import datetime
import time
import os
import base64
from utils.network import Network
from utils.filesystem import Filesystem
from dataclasses import dataclass
from typing import List, Optional, TypedDict


class Image(TypedDict):
    filepath: str
    url: Optional[str]


class Timings(TypedDict):
    job_time_received: str
    job_time_queued: str
    job_time_processed: str
    job_time_completed: str
    job_time_total: int


class Result(TypedDict):
    images: List[Image]
    timings: Timings


class BaseHandler:
    ENDPOINT_PROMPT = "http://127.0.0.1:18188/prompt"
    ENDPOINT_QUEUE = "http://127.0.0.1:18188/queue"
    ENDPOINT_HISTORY = "http://127.0.0.1:18188/history"
    INPUT_DIR = f"/opt/ComfyUI/input/"
    OUTPUT_DIR = f"/opt/ComfyUI/output/"

    workflow_file: str = None

    request_id = None
    comfyui_job_id = None

    job_time_queued: datetime = None
    job_time_processed: datetime = None
    job_time_completed: datetime = None
    result: Result = None

    def __init__(self, payload, workflow_file: str = None):
        self.job_time_received = datetime.datetime.now()
        self.payload = payload
        self.workflow_file = workflow_file

        self.request_id = str(self.get_value(
            "request_id",
            None
        )
        )
        self.set_prompt()

    def set_prompt(self):
        if self.workflow_file:
            with open(self.workflow_file, 'r') as f:
                self.prompt = {"prompt": json.load(f)}
        else:
            self.prompt = {"prompt": self.payload["workflow_json"]}

    def get_value(self, key, default: any = None):
        if key not in self.payload and default == None:
            raise IndexError(f"{key} required but not set")
        elif key not in self.payload:
            return default
        elif Network.is_url(self.payload[key]) and not (key.startswith("aws_") or key.startswith("webhook_")):
            return self.get_url_content(self.payload[key])
        else:
            return self.payload[key]

    def get_input_dir(self):
        return f"{self.INPUT_DIR}"

    def get_output_dir(self):
        return f"{self.OUTPUT_DIR}"

    def replace_urls(self, data):
        if isinstance(data, dict):
            for key, value in data.items():
                data[key] = self.replace_urls(value)
        elif isinstance(data, list):
            for i, item in enumerate(data):
                data[i] = self.replace_urls(item)
        elif isinstance(data, str) and Network.is_url(data):
            data = self.get_url_content(data)
        return data

    def get_url_content(self, url):
        existing_file = Filesystem.find_input_file(
            self.get_input_dir(),
            Network.get_url_hash(url)
        )
        if existing_file:
            return os.path.basename(existing_file)
        else:
            return os.path.basename(Network.download_file(
                url,
                self.get_input_dir(),
                self.request_id
            )
            )

    def is_server_ready(self):
        try:
            req = requests.head(self.ENDPOINT_PROMPT)
            return True if req.status_code == 200 else False
        except:
            return False

    def queue_job(self, timeout=30):
        try:
            self.job_time_queued = datetime.datetime.now()
            while ((datetime.datetime.now() - self.job_time_queued).seconds < timeout) and not self.is_server_ready():
                print(f"waiting for local server...")
                time.sleep(0.5)

            if not self.is_server_ready():
                self.invoke_webhook(success=False, error=f"Server not ready after timeout ({timeout}s)")
                raise requests.RequestException(f"Server not ready after timeout ({timeout}s)")

            print("Posting job to local server...")
            data = json.dumps(self.prompt).encode('utf-8')
            response = requests.post(self.ENDPOINT_PROMPT, data=data).json()
            if "prompt_id" in response:
                return response["prompt_id"]
            elif "node_errors" in response:
                self.invoke_webhook(success=False, error=response["node_errors"])
                raise requests.RequestException(response["node_errors"])
            elif "error" in response:
                self.invoke_webhook(success=False, error=response["error"])
                raise requests.RequestException(response["error"])
        except requests.RequestException:
            self.invoke_webhook(success=False, error="Unknown error")
            raise
        except:
            self.invoke_webhook(success=False, error="Unknown error")
            raise requests.RequestException("Failed to queue prompt")

    def get_job_status(self):
        try:
            history = requests.get(self.ENDPOINT_HISTORY).json()
            if self.comfyui_job_id in history:
                self.job_time_processed = datetime.datetime.now()
                return "complete"
            queue = requests.get(self.ENDPOINT_QUEUE).json()
            for job in queue["queue_running"]:
                if self.comfyui_job_id in job:
                    return "running"
            for job in queue["queue_pending"]:
                if self.comfyui_job_id in job:
                    return "pending"
        except Exception as e:
            self.invoke_webhook(success=False, error="Failed to queue job")
            raise requests.RequestException("Failed to queue job")

    def image_to_base64(self, path):
        with open(path, "rb") as f:
            b64: bytes = (base64.b64encode(f.read()))
        return "data:image/png;charset=utf-8;base64, " + b64

    def get_result(self, job_id):
        result = requests.get(self.ENDPOINT_HISTORY).json()[self.comfyui_job_id]

        self.result: Result = {
            "images": [],
            "timings": {}
        }

        outputs = result["outputs"]
        for key, value in outputs.items():
            for inner_key, inner_value in value.items():
                if isinstance(inner_value, list):
                    for item in inner_value:
                        if item.get("type") == "output":
                            original_path = os.path.join(self.OUTPUT_DIR, item['subfolder'], item['filename'])
                            self.result["images"].append({
                                "filepath": original_path
                            })

        self.job_time_completed = datetime.datetime.now()
        self.result["timings"] = {
            "job_time_received": self.job_time_received.ctime(),
            "job_time_queued": self.job_time_queued.ctime(),
            "job_time_processed": self.job_time_processed.ctime(),
            "job_time_completed": self.job_time_completed.ctime(),
            "job_time_total": (self.job_time_completed - self.job_time_received).seconds
        }

        return self.result

    # Webhook cannot be mandatory. Quick fix
    def invoke_webhook(self, success=False, result: Result=None, error=""):
        if result is None:
            result = {}
        try:
            webhook_url = self.get_value("webhook_url", os.environ.get("WEBHOOK_URL"))
        except:
            return None
        webhook_extra_params = self.get_value("webhook_extra_params", {})

        if Network.is_url(webhook_url):
            data: dict = {"job_id": self.comfyui_job_id, "request_id": self.request_id, "success": success}
            if result:
                data["result"] = result
            if error:
                data["error"] = error
            if webhook_extra_params:
                data["extra_params"] = webhook_extra_params
            Network.invoke_webhook(webhook_url, data)
        else:
            print("webhook_url is NOT valid!")

    def handle(self):
        self.comfyui_job_id = self.queue_job(30)

        status = None
        while status != "complete":
            status = self.get_job_status()
            if status != "complete":
                print(f"Waiting for {status} job to complete")
                time.sleep(0.5)

        result = self.get_result(self.comfyui_job_id)
        self.invoke_webhook(success=True, result=result)
        return result
