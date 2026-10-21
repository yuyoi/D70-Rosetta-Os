Put your own D-70 v1.11 EPROM dumps here (they are ignored by git, never commit them):

- `IC4_even.bin` - the EVEN EPROM (IC4), 64 KB
- `IC9_odd.bin` - the ODD EPROM (IC9), 64 KB

With a T48 / TL866: `minipro -p "AM27C512@DIP28" -r dumps/IC4_even.bin` (read twice and compare).
