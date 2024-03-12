import json
import uuid
import requests  # Added for making HTTP requests

import boto3
from botocore.exceptions import ClientError

dynamodb = boto3.resource('dynamodb')
textract_client = boto3.client('textract')
s3 = boto3.client('s3')
table_name = 'FilesTable'  # DynamoDB table name
bucket_name = 'FilesForTextract'  # S3 bucket name


def get_textract_results(file_id):
    table = dynamodb.Table(table_name)

    try:
        response = table.get_item(
            Key={
                'file_id': file_id
            },
            ProjectionExpression="file_id, textract_result"
        )
        item = response.get('Item')
        return item
    except ClientError as e:
        print(f"Error retrieving data from DynamoDB: {e}")
        return None


def get_textract_info(event, context):
    file_id = event['pathParameters']['file_id']

    textract_results = get_textract_results(file_id)

    if textract_results:
        response = {
            "statusCode": 200,
            "body": json.dumps(textract_results)
        }
    else:
        response = {
            "statusCode": 404,
            "body": json.dumps({"error": "File not found"})
        }

    return response


# The POST logic

def generate_presigned_url(file_id):
    try:
        url = s3.generate_presigned_url(
            'put_object',
            Params={
                'Bucket': bucket_name,
                'Key': file_id
            },
            ExpiresIn=3600  # Adjust the expiration time as needed
        )
        return url
    except ClientError as e:
        print(f"Error generating presigned URL: {e}")
        return None


def create_file(event, context):
    callback_url = json.loads(event['body']).get('callback_url')
    file_id = str(uuid.uuid4())  # Generate a unique file ID
    presign_url = generate_presigned_url(file_id)

    if presign_url:
        # Save data to DynamoDB
        dynamodb.Table(table_name).put_item(
            Item={
                'file_id': file_id,
                'callback_url': callback_url,
                'presign_url': presign_url
            }
        )

        # Return presigned URL for file upload
        response = {
            "statusCode": 200,
            "body": json.dumps({"upload_url": presign_url})
        }

    else:
        response = {
            "statusCode": 500,
            "body": json.dumps({"error": "Error generating presigned URL"})
        }

    return response


# TEXTRACT logic

def update_dynamodb(file_id, textract_result):
    table = dynamodb.Table(table_name)

    try:
        response = table.update_item(
            Key={
                'file_id': file_id
            },
            UpdateExpression="set #attrName = :r",
            ExpressionAttributeNames={
                '#attrName': 'textract_result'
            },
            ExpressionAttributeValues={
                ':r': textract_result
            },
            ReturnValues="UPDATED_NEW"
        )
        return response
    except ClientError as e:
        print(f"Error updating DynamoDB item: {e}")
        return None


def process_file(event, context):
    """
    process_file handler function
    param: event: The event object for the Lambda function.
    param: context: The context object for the lambda function.
    return: The list of Block objects recognized in the document
    passed in the event object.
    """

    try:
        # Retrieve the bucket and object key from the S3 event
        bucket = event['Records'][0]['s3']['bucket']['name']
        key = event['Records'][0]['s3']['object']['key']

        # Get the S3 object
        s3_object = s3.get_object(Bucket=bucket, Key=key)

        # Read the content of the object
        image_bytes = s3_object['Body'].read()

        # Analyze the document.
        response = textract_client.detect_document_text(Document={'Bytes': image_bytes})

        # Get the Blocks
        blocks = response['Blocks']

        # Extract relevant information from Textract result
        textract_result = json.dumps(blocks)

        # Retrieve file_id from key (assuming the key contains the file_id)
        file_id = key

        # Update DynamoDB item with Textract result
        update_dynamodb(file_id, textract_result)

        lambda_response = {
            "statusCode": 200,
            "body": json.dumps(blocks)
        }

    except ClientError as err:
        error_message = "Couldn't analyze image. " + err.response['Error']['Message']

        lambda_response = {
            'statusCode': 400,
            'body': {
                "Error": err.response['Error']['Code'],
                "ErrorMessage": error_message
            }
        }

    return lambda_response


# THE CALLBACK logic

def get_textract_results_for_callback(file_id):
    table = dynamodb.Table(table_name)

    try:
        response = table.get_item(
            Key={
                'file_id': file_id
            },
            ProjectionExpression="file_id, textract_result, callback_url"
        )
        item = response.get('Item')
        return item
    except ClientError as e:
        print(f"Error retrieving data from DynamoDB: {e}")
        return None


def make_callback(event, context):
    for record in event['Records']:
        if record['eventName'] == 'MODIFY':
            # Get the file_id from the DynamoDB stream event
            file_id = record['dynamodb']['Keys']['file_id']['S']

            # Get the updated item from DynamoDB
            updated_item = get_textract_results_for_callback(file_id)

            if updated_item and 'callback_url' in updated_item:
                # Extract relevant information from the updated item
                callback_url = updated_item['callback_url']
                textract_result = updated_item.get('textract_result', [])

                # Check if textract_result is not empty
                if textract_result:
                    # Send response using the callback_url
                    send_callback_response(callback_url, file_id, textract_result)


def send_callback_response(callback_url, file_id, textract_result):

    try:
        response = requests.post(callback_url, json={"file_id": file_id, "textract_result": textract_result})
        response.raise_for_status()
        print("Callback response sent successfully.")
    except requests.exceptions.RequestException as e:
        print(f"Error sending callback response: {e}")
