# Terminals, icons, and the Windows console

Why MeshTerm looks right in some terminals and draws empty boxes in others, what it does
about it on Windows, and how to turn all of that off.

MeshTerm is drawn with emoji icons, braille charts and powerline path chips. Whether you
see them is up to your *terminal*, not really your font: a modern terminal, asked for a
character its font doesn't have, quietly borrows it from another font on the machine.
That's why the app looks right in Windows Terminal, in VS Code's terminal, and on macOS
and Linux — usually with a font that contains almost none of it.

The classic Windows console — the black `cmd.exe` window you get from a double-click —
doesn't borrow. It draws what its one font holds and empty boxes for everything else, and
no font fixes the icons there: the only two fonts on a Windows machine with emoji in them
are proportional, and a console won't take a proportional font.

So when MeshTerm lands in that console it just **moves to Windows Terminal** — it says
so, and opens there. Nothing is installed and nothing is changed; it's one window instead
of another, and it's the whole app exactly as the screenshots show it. Windows Terminal is
already on every Windows 11 machine and is a free install on Windows 10.

Where there's no Windows Terminal to move to, MeshTerm offers the next best thing instead
— the charts and marks, without the icons. It ships
[Cascadia Mono PL](https://github.com/microsoft/cascadia-code), Microsoft's own console
font and one of the very few monospace faces that carries braille at all, and will install
it just for you: no administrator rights, nothing downloaded. If you already have Cascadia
it simply switches to it.

Preferences → Display → Console setup turns all of this off if you'd rather stay put.

Where MeshTerm keeps its state, and how to point a trial run somewhere else, is in
[Configuring MeshTerm](configuration.md).
