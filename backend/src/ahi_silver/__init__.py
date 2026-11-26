"""Bronze -> Silver rules (AHI scenario document), as pure functions.

Column matching (saved -> exact -> fuzzy -> word2vec -> Gemini), cleansing (trim,
blanks to NULL, dates, decimals), profit-center population from the LOTL, and the
business-key / row hashes. Nothing here touches the database; the API service reads
bronze, runs these, lets a person approve the mapping, and loads Silver.
"""
