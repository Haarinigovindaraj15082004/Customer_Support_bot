from db import init_db
from agent import chat_turn

def main():
    init_db()
    print("Cassie chat demo. Type 'quit' to exit.\n")
    session_id = "local_demo_session"   

    while True:
        user = input("you: ")
        if user.strip().lower() in ("quit", "exit"):
            break

        reply, ticket_id = chat_turn(session_id, user, email=None, name="CLI User")
        if ticket_id:
            print(f"cassie: {reply}  (ticket #{ticket_id})")
        else:
            print(f"cassie: {reply}")

if __name__ == "__main__":
    main()
