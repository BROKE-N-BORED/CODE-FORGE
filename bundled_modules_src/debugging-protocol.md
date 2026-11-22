# Debugging protocol

## The engine

\- The debugger runs one Python file under pdb, the debugger in the Python standard library.
\- It runs in a subprocess, driven over its input and output pipes.
\- pdb ships with every Python. Debugging adds no new dependencies.
\- One session drives one target file.
\- The debugger is waiting for a command when its output ends with the prompt (Pdb) followed by a space, with no newline after it.
\- Each command waits until the prompt, the end of the output, or a timeout settles what the command produced.

## Session states

Every debug call returns a state. Read the state before you act.

\- stopped: the debugger sits at a place in the code. The state names the file, the line, and the function.
\- finished: the program ran to its end. The session is closed and the subprocess is gone.
\- running: the program is still executing. Step and inspect refuse until it stops. Stop always works.
\- error: the call could not be made. Example causes: no live session; the target is outside the workspace.

## Rules

\- A target outside the workspace is refused before anything starts.
\- Refusals come back as an error state, not as a crash.
\- When the program finishes, the engine quits pdb itself. A finished program never leaves a session sitting open on line 1.
\- Stop kills the whole process group. A stopped session never leaks a process.

## Working discipline

\- Build from gathered context, not from the raw one-line request.
\- Read the files your change touches before your first edit.
\- Check the repo map. Use code_graph to find where a symbol is defined and who uses it.
\- State in your reply what you read, before the first edit lands.
\- An approved plan document in the conversation is your working instruction. Build from the document, not from memory of the request.
\- A specific the owner has not pinned down, one that would change the build: name it in your reply as an explicit assumption. Never invent it silently.
