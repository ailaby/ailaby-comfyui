import os
import shutil

from handlers.basehandler import BaseHandler
from utils.s3utils import s3utils


class S3BaseHandler(BaseHandler):

    def __init__(self, payload, workflow_file: str = None):
        super().__init__(payload, workflow_file)
        self.s3utils = s3utils(self.get_s3_settings())

    def get_result(self, job_id):
        result = super().get_result(job_id)

        custom_output_dir = f"{self.OUTPUT_DIR}{self.request_id}"
        os.makedirs(custom_output_dir, exist_ok=True)

        for image in result['images']:
            filepath = image['filepath']
            new_path = f"{custom_output_dir}/{os.path.basename(filepath)}"

            # Handle duplicated request where output file is not re-generated
            if os.path.islink(filepath):
                shutil.copyfile(os.path.realpath(filepath), new_path)
            else:
                os.rename(filepath, new_path)
                os.symlink(new_path, filepath)
            key = f"{self.request_id}/{os.path.basename(filepath)}"
            image["url"] = self.s3utils.file_upload(new_path, key)

        return result

    def get_s3_settings(self):
        settings = {}
        settings["aws_access_key_id"] = self.get_value("aws_access_key_id", os.environ.get("AWS_ACCESS_KEY_ID"))
        settings["aws_secret_access_key"] = self.get_value("aws_secret_access_key",
                                                           os.environ.get("AWS_SECRET_ACCESS_KEY"))
        settings["aws_endpoint_url"] = self.get_value("aws_endpoint_url", os.environ.get("AWS_ENDPOINT_URL"))
        settings["aws_bucket_name"] = self.get_value("aws_bucket_name", os.environ.get("AWS_BUCKET_NAME"))
        settings["connect_timeout"] = 5
        settings["connect_attempts"] = 1
        return settings
