# Caption fonts

`marcellus-sc/MarcellusSC-Regular.ttf` is the current `game_of_thrones`
caption font. It is pinned from the official Google Fonts repository. Its
expected SHA-256 is:

```text
03044ab65228b0554c9a5c1e63df29d4e58a71857e212801aaf768dfd106bdf0
```

Marcellus SC replaces Cinzel as the automatic GOT/HOTD choice because its
Roman-capital design keeps the cinematic title character while remaining
cleaner in four-word mobile subtitle cards.

`cinzel/Cinzel-Variable.ttf` remains bundled as the explicit
`game_of_thrones_cinzel` comparison/rollback theme. Its expected SHA-256 is:

```text
f4d83d34d1f6c741193e4acf4b3dff9531e5a67b6aa65228d00a7db72a4e0f34
```

Both fonts are distributed under the SIL Open Font License 1.1; the
corresponding license text is stored beside each font.

In single-file Colab mode, where this asset directory is not uploaded with the
notebook, the render pipeline downloads this exact official file once into the
Drive-backed render font cache and rejects it if the SHA-256 differs. It never
silently substitutes another font for a themed render.





