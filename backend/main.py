from app.gmail.client import GmailClient


gmail = GmailClient()

messages, next_page_token = gmail.list_messages(
    max_results=10
)

for message in messages:

    metadata = gmail.get_message(
        message["id"]
    )

    print("=" * 70)

    print("ID:", metadata["id"])
    print("Thread:", metadata["threadId"])
    print("Snippet:", metadata.get("snippet"))

    print("\nHEADERS:")

    for header in metadata["payload"]["headers"]:
        print(
            header["name"],
            ":",
            header["value"]
        )