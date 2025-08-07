from langchain_community.agent_toolkits import create_sql_agent
from langchain_community.utilities import SQLDatabase
from langchain.agents import AgentType
from langchain_openai import ChatOpenAI
import os
from dotenv import load_dotenv

# 1. Load environment variables
print("🔹 Loading .env variables...")
load_dotenv()

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
if not OPENAI_API_KEY:
    raise ValueError("❌ OPENAI_API_KEY not found in .env")
else:
    print("✅ OPENAI_API_KEY loaded.")

# 2. Connect to your Supabase PostgreSQL database
print("🔹 Connecting to Supabase Postgres...")

DB_URI = os.getenv("DB_URI")

if not DB_URI:
    raise ValueError("❌ DB_URI not found in .env")
else:
    print("✅ DB_URI loaded.")

try:
    db = SQLDatabase.from_uri(DB_URI)
    print("✅ Connected to Supabase PostgreSQL")
except Exception as e:
    print("❌ Failed to connect to DB:", e)
    raise

# 3. Initialize your LLM (e.g., OpenAI GPT-3.5)
print("🔹 Initializing ChatOpenAI...")
llm = ChatOpenAI(model="gpt-3.5-turbo", temperature=0)
print("✅ ChatOpenAI loaded with model gpt-3.5-turbo")

# 4. Create the SQL Agent
print("🔹 Creating SQL Agent...")
try:
    sql_agent = create_sql_agent(
        llm=llm,
        db=db,
        agent_type=AgentType.ZERO_SHOT_REACT_DESCRIPTION,
        verbose=True
    )
    print("✅ SQL Agent created successfully")
except Exception as e:
    print("❌ Failed to create SQL agent:", e)
    raise

# 5. Run SQL queries via natural language
print("🤖 Asking: Which day had the highest total PnL?")
try:
    response = sql_agent.run("Which day had the highest total PnL?")
    print("🟢 Response:\n", response)
except Exception as e:
    print("❌ Failed to run query:", e)

print("\n🤖 Asking: Show the 3 most profitable trades")
try:
    response = sql_agent.run("Show the 3 most profitable trades")
    print("🟢 Response:\n", response)
except Exception as e:
    print("❌ Failed to run query:", e)
