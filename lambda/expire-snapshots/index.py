def lambda_handler(event, context):
    print("Expire snapshots invoked")
    return {"status": "ok"}
