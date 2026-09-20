# Summary Draft:

Our project is based on building a serverless e-commerce application that detects anomalies and failures.
Our goal is to demonstrate cloud monitoring, machine learning and AI to portray incidents. 
We will have an API Gateway that exposes the application APIs. 
With our project, the AWS Lambda will run the application services and data will be stored with DynamoDB. 
To collect logs, we will use CloudWatch. In order to store telemetry,  Amazon S3 would be used. 
The collected telemetry will be used for the machine learning model to classify the various types of incidents.
In order generate a readable metrics of the incidents, we will be using Bedrock. 
Lastly, we will test our system with traffic spickes, slowdowns and dependency errors. 
