from patent_client import USApplication
print(list(USApplication.objects.filter(patent_title="artificial intelligence").limit(3)))
