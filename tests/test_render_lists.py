import unittest

from stepik_mcp.render import html_to_md, md_to_html


class ListParagraphTests(unittest.TestCase):
    """Stepik's editor wraps list items in <p>; they used to become empty '- ' items."""

    def test_paragraph_inside_item(self):
        source = "<ul><li><p>один</p></li><li><p><strong>два</strong> и всё</p></li></ul>"
        self.assertEqual(html_to_md(source), "- один\n- **два** и всё")

    def test_ordered_list_with_paragraphs(self):
        source = "<ol><li><p>первый</p></li><li><p>второй</p></li></ol>"
        self.assertEqual(html_to_md(source), "1. первый\n2. второй")

    def test_second_paragraph_stays_in_item(self):
        self.assertEqual(html_to_md("<ul><li><p>a</p><p>b</p></li></ul>"), "- a  \n  b")

    def test_nested_list_has_no_blank_line(self):
        source = "<ul><li><p>a</p><ul><li><p>b</p></li></ul></li><li>c</li></ul>"
        self.assertEqual(html_to_md(source), "- a\n  - b\n- c")
        self.assertEqual(md_to_html(html_to_md(source)), "<ul><li>a<ul><li>b</li></ul></li><li>c</li></ul>")

    def test_legacy_mode_keeps_old_output(self):
        # Old local files were produced this way; publishing compares against it.
        self.assertEqual(html_to_md("<ul><li><p>один</p></li></ul>", legacy=True), "- \n\nодин")

    def test_consecutive_code_blocks_both_have_fences(self):
        # Прежний загрузчик терял открывающую ``` у второго блока подряд.
        source = "<pre><code>a</code></pre><pre><code>b</code></pre>"
        self.assertEqual(html_to_md(source), "```\na\n```\n\n```\nb\n```")
        self.assertEqual(html_to_md(source, legacy=True), "```\na\n```\n\nb\n```")

    def test_code_with_backticks_gets_longer_fence(self):
        markdown = html_to_md('<pre><code class="language-md">```py\nx\n```</code></pre>')
        self.assertEqual(markdown, "````md\n```py\nx\n```\n````")
        self.assertEqual(md_to_html(markdown), '<pre><code class="language-md">```py\nx\n```</code></pre>')

    def test_markup_inside_code_block_is_plain_text(self):
        self.assertEqual(html_to_md("<pre><strong>Итог:</strong><br>x</pre>"), "```\nИтог:\nx\n```")

    def test_literal_backticks_in_text_do_not_open_code(self):
        markdown = html_to_md("<p>```html</p>")
        self.assertEqual(markdown, "\\`\\`\\`html")
        self.assertEqual(md_to_html(markdown), "<p>```html</p>")

    def test_blockquote_keeps_its_content(self):
        # Врезки уроков: текст выпадал из цитаты, а цитата оставалась пустой.
        source = "<blockquote><p><strong>🔑 Итог:</strong><br>\nвторая строка</p></blockquote>"
        markdown = html_to_md(source)
        self.assertEqual(markdown, "> **🔑 Итог:**  \n> вторая строка")
        self.assertEqual(md_to_html(markdown), "<blockquote><p><strong>🔑 Итог:</strong><br>\nвторая строка</p></blockquote>")

    def test_blockquote_with_two_paragraphs_and_list(self):
        source = "<blockquote><p>a</p><ul><li>b</li></ul><p>c</p></blockquote>"
        self.assertEqual(html_to_md(source), "> a\n>\n> - b\n>\n> c")
        self.assertEqual(md_to_html(html_to_md(source)), "<blockquote><p>a</p>\n<ul><li>b</li></ul>\n<p>c</p></blockquote>")

    def test_old_quote_mode_reproduces_format_2(self):
        self.assertEqual(html_to_md("<blockquote><p>x</p></blockquote>", old_quotes=True), "> \n\nx")

    def test_literal_asterisks_stay_text(self):
        markdown = html_to_md("<p>2**3 и a*b*c</p>")
        self.assertEqual(md_to_html(markdown), "<p>2**3 и a*b*c</p>")

    def test_bold_code_keeps_bold(self):
        source = "<p><strong><code>await</code></strong> и <em>курсив <code>x</code></em></p>"
        self.assertEqual(md_to_html(html_to_md(source)), source)

    def test_paragraphs_outside_lists_unchanged(self):
        self.assertEqual(html_to_md("<p>a</p><p>b</p>"), "a\n\nb")


if __name__ == "__main__":
    unittest.main()
