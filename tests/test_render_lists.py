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
        self.assertEqual(html_to_md("<blockquote><p>x</p></blockquote>", fmt=2), "> \n\nx")

    def test_table_kept_as_html(self):
        # Таблицы терялись при правке шага: в Markdown урока не было их записи.
        table = '<table border="1"><tbody><tr><td><p>a &lt; b</p></td><td>**x**</td></tr></tbody></table>'
        markdown = html_to_md(f"<p>до</p>{table}<p>после</p>")
        self.assertEqual(markdown, f"до\n\n{table}\n\nпосле")
        self.assertEqual(md_to_html(markdown), f"<p>до</p>\n{table}\n<p>после</p>")

    def test_table_with_blank_lines_stays_one_block(self):
        markdown = "<table>\n<tr><td>a</td></tr>\n\n# не заголовок\n</table>"
        self.assertEqual(md_to_html(markdown), markdown)

    def test_details_with_markdown_inside(self):
        source = ('<details><summary><strong>Ответ</strong></summary><p>Текст <code>x</code></p>'
                  '<ul><li>один</li></ul></details>')
        markdown = html_to_md(source)
        self.assertEqual(markdown, "<details>\n<summary>**Ответ**</summary>\n\nТекст `x`\n\n- один\n\n</details>")
        self.assertEqual(md_to_html(markdown), '<details><summary><strong>Ответ</strong></summary>'
                                               '<p>Текст <code>x</code></p>\n<ul><li>один</li></ul></details>')

    def test_details_keeps_attributes_and_works_in_quote(self):
        source = "<blockquote><p>Q</p><details open><summary>S</summary><p>h</p></details></blockquote>"
        self.assertEqual(md_to_html(html_to_md(source)),
                         "<blockquote><p>Q</p>\n<details open><summary>S</summary><p>h</p></details></blockquote>")

    def test_old_blocks_mode_reproduces_format_3(self):
        self.assertEqual(html_to_md("<table><tr><td>a</td></tr></table>", fmt=3), "a")

    def test_format_5_keeps_styles_sizes_links(self):
        source = ('<h2 style="text-align:center;">Урок</h2>'
                  '<pre style="background:#2d3436; color:#ffffff;"><code class="language-python">x = 1</code></pre>'
                  '<p>См. <a href="https://docs.python.org">документацию</a> и '
                  '<img alt="схема" src="https://x/s.png" width="870"></p>')
        markdown = html_to_md(source)
        self.assertIn('<h2 style="text-align:center;">Урок</h2>', markdown)
        self.assertIn('```python {style="background:#2d3436; color:#ffffff;"}', markdown)
        self.assertIn("[документацию](https://docs.python.org)", markdown)
        self.assertIn('![схема](https://x/s.png){width="870"}', markdown)
        back = md_to_html(markdown)
        self.assertIn('<pre style="background:#2d3436; color:#ffffff;"><code class="language-python">x = 1</code></pre>', back)
        self.assertIn('<a href="https://docs.python.org">документацию</a>', back)
        self.assertIn('<img alt="схема" src="https://x/s.png" width="870">', back)
        self.assertNotIn("style", html_to_md(source, fmt=4).split("```")[1].split("\n")[0])

    def test_space_inside_bold_moves_outside(self):
        # «**Вопрос: **» в Markdown не жирный: на Stepik ушли бы звёздочки.
        for space in (" ", "\xa0"):
            source = f"<p><strong>Вопрос:{space}</strong>Текст и <em> курсив</em></p>"
            back = md_to_html(html_to_md(source))
            self.assertIn("<strong>Вопрос:", back)
            self.assertIn("<em>курсив</em>", back)
            self.assertNotIn("**", back)

    def test_double_break_and_nbsp_survive(self):
        source = "<p>a<br><br>b</p><h3>\xa0</h3><p>\xa0</p>"
        markdown = html_to_md(source)
        self.assertEqual(md_to_html(markdown), "<p>a<br>\n<br>\nb</p>\n<h3>&nbsp;</h3>\n<p>&nbsp;</p>")

    def test_text_that_looks_like_markup_stays_text(self):
        source = "<blockquote><p><strong>Цель:</strong><br>- первое<br>1. второе<br># третье</p></blockquote>"
        back = md_to_html(html_to_md(source))
        self.assertNotIn("<ul>", back)
        self.assertNotIn("<ol>", back)
        self.assertNotIn("<h1>", back)
        self.assertIn("- первое", back)
        self.assertIn("1. второе", back)

    def test_break_inside_list_item(self):
        source = "<ul><li><code>a</code> — первое.<br>Пример: <code>b</code></li></ul>"
        self.assertEqual(md_to_html(html_to_md(source)), "<ul><li><code>a</code> — первое.<br>Пример: <code>b</code></li></ul>")

    def test_code_block_inside_list_item(self):
        source = ('<ol><li><strong>Шаг</strong>: запустите<pre style="background:#000;"><code class="language-python">'
                  'a = 1\n\nprint(a)</code></pre></li><li>второй</li></ol>')
        markdown = html_to_md(source)
        self.assertEqual(markdown, '1. **Шаг**: запустите\n  ```python {style="background:#000;"}\n  a = 1\n\n'
                                   '  print(a)\n  ```\n2. второй')
        self.assertEqual(md_to_html(markdown), source)

    def test_code_block_in_nested_list_and_text_after_it(self):
        source = "<ul><li>a<ul><li>b<pre><code>x</code></pre></li></ul></li><li>c</li></ul>"
        self.assertEqual(md_to_html(html_to_md(source)), source)

    def test_paragraph_after_code_in_list_item(self):
        source = "<ul><li><p>Пример:</p><pre><code>x</code></pre><p><strong>Вывод:</strong></p><pre><code>y</code></pre></li></ul>"
        self.assertEqual(md_to_html(html_to_md(source)),
                         "<ul><li>Пример:<pre><code>x</code></pre> <strong>Вывод:</strong><pre><code>y</code></pre></li></ul>")

    def test_format_5_keeps_code_after_list(self):
        markdown = html_to_md("<ul><li>a<pre><code>x</code></pre></li></ul>", fmt=5)
        self.assertEqual(markdown, "- a\n\n```\nx\n```")

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
