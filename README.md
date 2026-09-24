# Summary Draft

Our project is based on building a serverless e-commerce application that detects anomalies and failures.
Our goal is to demonstrate cloud monitoring, machine learning, and AI to portray incidents.
We will have an API Gateway that exposes the application APIs.
With our project, AWS Lambda will run the application services and data will be stored with DynamoDB.
To collect logs, we will use CloudWatch. To store telemetry, Amazon S3 would be used.
The collected telemetry will be used for the machine learning model to classify the various types of incidents.
In order to generate readable metrics of the incidents, we will be using Bedrock.
Finally, we will evaluate the system by simulating different failure scenarios, including traffic spikes, performance slowdowns, and downstream dependency failures.
