from ollama import chat
from ollama import ChatResponse
import subprocess
#import voice

messages = []
#def main(a):
while True:
    a = input("Me: ")
    if a.lower() == "bye":
        print("Exiting chat. Goodbye!")
        break
    messages.append({'role': 'user', 'content': a})
        
    # Get response from the Ollama model
    response: ChatResponse = chat(model='deeprina:latest', messages=messages)

    # Append the model's response to the messages list
    messages.append({'role': 'assistant', 'content': response.message.content})

    thought = response.message.content.split("</think>", 1)[0].strip()
    reply = response.message.content.split("</think>", 1)[-1].strip()

    # Print the response
    print(f"Rina-chan thought: {thought}")
    print(f"Rina-chan speech: {reply}")
        #print(response.message.content)

        #command = f"echo \"{response['message']['content']}\" | festival --tts"
        #subprocess.run(command, shell=True)
    #voice.main(reply)

