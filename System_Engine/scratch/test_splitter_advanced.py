from services.text_splitter import TextSplitter

def test_splitter():
    splitter = TextSplitter(chunk_size=100, overlap=10)

    # 1. Test Code Block Protection
    text_code = "Intro text.\n```python\ndef hello():\n    print('This code block is very long and should not be split in the middle even if it exceeds the chunk size.')\n```\nOutro text."
    chunks = splitter.split_text(text_code)
    print(f"Code Test: {len(chunks)} chunks")
    for i, c in enumerate(chunks):
        print(f"Chunk {i+1} Ends with: {repr(c[-20:])}")
        # Ensure '```' count is even in each chunk
        assert c.count('```') % 2 == 0

    # 2. Test Table Protection
    text_table = "Intro.\n| Name | Value |\n|------|-------|\n| Long Row Content That Exceeds The Limit | 1234567890 |\n| Another Row |\nOutro."
    chunks = splitter.split_text(text_table)
    print(f"\nTable Test: {len(chunks)} chunks")
    # Table should stay together or split at newline
    for c in chunks:
        lines = c.split('\n')
        for l in lines:
            if '|' in l:
                assert l.startswith('|') and l.endswith('|')

    # 3. Test Paragraph Priority
    text_para = "Short para.\n\n" + "A" * 80 + "\n\n" + "B" * 80
    chunks = splitter.split_text(text_para)
    print(f"\nPara Test: {len(chunks)} chunks")
    # It should split at \n\n if possible
    assert any("\n\n" in c for c in chunks)

if __name__ == "__main__":
    test_splitter()
