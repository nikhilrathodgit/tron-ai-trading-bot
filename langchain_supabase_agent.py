from langchain.agents import initialize_agent, Tool
from langchain_openai import ChatOpenAI
from supabase import create_client
import os
from dotenv import load_dotenv

# Load environment variables
load_dotenv()

SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")

if not SUPABASE_URL or not SUPABASE_KEY:
    raise ValueError("❌ Missing Supabase credentials in .env")
if not OPENAI_API_KEY:
    raise ValueError("❌ Missing OpenAI API key in .env")

print("🔹 Connecting to Supabase...")
supabase = create_client(SUPABASE_URL, SUPABASE_KEY)
print("✅ Connected to Supabase")

# Trade functions
def get_last_5_trades(query: str = None):
    res = (
        supabase.table("trades")
        .select("*")
        .order("entry_time", desc=True)
        .limit(5)
        .execute()
    )

    if not res.data:
        return "No trades found"

    # Return in a simple readable format
    return "\n".join(
        [f"[{t['id']}] {t['action']} {t['amount']} @ {t['entry_price']}" for t in res.data]
    )


def get_pnl(query: str = None):
    res = supabase.table("trades").select("pnl").not_.is_("pnl", None).execute()
    pnls = [t["pnl"] for t in res.data if t["pnl"] is not None]
    total = sum(pnls) if pnls else 0
    return f"Total PnL: {total:.4f}"


# Tools for Langchain
tools = [
    Tool(
        name="GetLast5Trades",
        func=get_last_5_trades,
        description="Fetch the 5 most recent trades from Supabase"
    ),
    Tool(
        name="GetPnL",
        func=get_pnl,
        description="Calculate total PnL from all trades"
    ),
]

# Initializing LangChain Agent
llm = ChatOpenAI(model="gpt-3.5-turbo", temperature=0)
agent = initialize_agent(tools, llm, agent="zero-shot-react-description", verbose=True, max_iterations=3)

# Testing agent
if __name__ == "__main__":
    print("\n🤖 Ask the AI bot something...\n")

    resp = agent.invoke("Show my last 3 trades")
    print("\nAgent Response:\n", resp)

    resp = agent.invoke("What is my total PnL?")
    print("\nAgent Response:\n", resp)
