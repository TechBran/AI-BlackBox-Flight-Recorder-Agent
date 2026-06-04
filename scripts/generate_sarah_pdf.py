import os
from reportlab.lib.pagesizes import letter
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.enums import TA_JUSTIFY, TA_LEFT

def generate_pdf(input_txt, output_pdf):
    if not os.path.exists(input_txt):
        print(f"Error: {input_txt} not found.")
        return

    with open(input_txt, 'r', encoding='utf-8') as f:
        content = f.read()

    doc = SimpleDocTemplate(output_pdf, pagesize=letter,
                            rightMargin=72, leftMargin=72,
                            topMargin=72, bottomMargin=72)

    styles = getSampleStyleSheet()
    
    # Custom styles
    title_style = ParagraphStyle(
        'TitleStyle',
        parent=styles['Heading1'],
        fontSize=18,
        spaceAfter=12,
        alignment=TA_LEFT
    )
    
    header_style = ParagraphStyle(
        'HeaderStyle',
        parent=styles['Heading2'],
        fontSize=14,
        spaceBefore=12,
        spaceAfter=6,
        alignment=TA_LEFT
    )
    
    body_style = ParagraphStyle(
        'BodyStyle',
        parent=styles['Normal'],
        fontSize=11,
        leading=14,
        alignment=TA_LEFT,
        spaceAfter=10
    )

    story = []
    
    lines = content.split('\n')
    for line in lines:
        line = line.strip()
        if not line:
            story.append(Spacer(1, 6))
            continue
            
        # Escape special XML characters for all lines
        escaped_line = line.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')

        if line.startswith('# '):
            story.append(Paragraph(escaped_line[2:], title_style))
        elif line.startswith('## '):
            story.append(Paragraph(escaped_line[3:], header_style))
        elif line.startswith('### '):
            story.append(Paragraph(escaped_line[4:], header_style))
        elif line.startswith('**') and line.endswith('**'):
            # Bold emphasis for application/questions
            story.append(Paragraph(f"<b>{escaped_line[2:-2]}</b>", body_style))
        else:
            # Handle basic markdown bold/italic if needed
            # We already escaped above, now handle bold pairs
            parts = escaped_line.split('**')
            clean_line = ""
            for i, part in enumerate(parts):
                clean_line += part
                if i < len(parts) - 1:
                    if i % 2 == 0:
                        clean_line += "<b>"
                    else:
                        clean_line += "</b>"
            story.append(Paragraph(clean_line, body_style))

    doc.build(story)
    print(f"Successfully generated {output_pdf}")

if __name__ == "__main__":
    input_file = "Apps/Bible_and_Pillow_Talk/Chapter_1_Sarah_Final_Draft.txt"
    output_file = "Apps/Bible_and_Pillow_Talk/Chapter_1_Sarah_Final_Draft.pdf"
    generate_pdf(input_file, output_file)
