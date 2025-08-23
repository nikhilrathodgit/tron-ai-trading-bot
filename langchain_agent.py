from langchain.agents import initialize_agent, load_tools
from langchain_openai import OpenAI
from langchain_openai import ChatOpenAI
from supabase import create_client
import os
from dotenv import load_dotenv

# Load environment variables from .env
load_dotenv()

print("🔹 Connecting to Supabase...")
supabase = create_client(os.getenv("SUPABASE_URL"), os.getenv("SUPABASE_KEY"))
print("✅ Connected to Supabase")

api_key = os.getenv("OPENAI_API_KEY")

# Step 2: Load tools
tools = load_tools(["serpapi"])  # dummy tool for now

# Step 3: Create simple agent

#llm = OpenAI(temperature=0) (will use this for final runs)
#using gpt-3.5 for cost efficency of API calls
llm = ChatOpenAI(model="gpt-3.5-turbo", temperature=0)
agent = initialize_agent(tools, llm, agent="zero-shot-react-description", verbose=True, max_iterations=3)

# Step 4: Ask it something
response = agent.run("Get BTC Price in USD")
print(response)
