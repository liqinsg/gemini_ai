#wsl                                   # from PowerShell or cmd
curl -s 127.0.0.1:11434 || (ollama serve > ~/ollama.log 2>&1 &)
sleep 3
cd ~/projects/gemini_ai
ollama launch dsh
