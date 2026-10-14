# The clock's font

`InterDisplayClock-SemiBold.ttf` is [Inter](https://rsms.me/inter/) Display
SemiBold from the Inter 4.1 release (`extras/ttf/` in `Inter-4.1.zip`), with
one change: the characters 0–9 and `:` draw Inter's fixed-width digits and
the colon centred on them. Without that, the clock would change width every
minute. It is renamed "Inter Display Clock" so it can't be taken for stock
Inter. Licence: SIL Open Font License 1.1, in `Inter-LICENSE.txt`.

To make it again:

```bash
uv run --with fonttools tools/make-clock-font.py \
    InterDisplay-SemiBold.ttf fonts/InterDisplayClock-SemiBold.ttf
```
