sudo apt install -y tmux          # once
~/start-dsh.sh ollama             # test step 1 alone
~/start-dsh.sh dsh                # then the harness
tmux attach -t dsh                # see the prompts; detach with Ctrl+B then D
