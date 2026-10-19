import json
import os
import argparse

def main():
    parser = argparse.ArgumentParser(description='按发票内容重命名文件')
    parser.add_argument('--invoice_dir', required=True, help='发票PDF所在目录')
    parser.add_argument('--data', required=True, help='发票数据JSON文件路径')
    args = parser.parse_args()
    
    with open(args.data, 'r', encoding='utf-8') as f:
        invoices = json.load(f)
    
    invoices.sort(key=lambda x: x['original_filename'])
    
    for i, inv in enumerate(invoices):
        old_path = os.path.join(args.invoice_dir, inv['original_filename'])
        items = inv.get('items', [])
        if items:
            first = items[0]
            cat = first.get('category', '未知')
            prod = first.get('product', '未知')
        else:
            cat = '未知'
            prod = '未知'
        total = inv.get('total', 0)
        new_name = f'发票-{i+1}-{cat}{prod}-{total}.pdf'
        new_path = os.path.join(args.invoice_dir, new_name)
        
        if os.path.exists(old_path):
            os.rename(old_path, new_path)
            print(f'  {inv["original_filename"]} -> {new_name}')
        else:
            print(f'  {inv["original_filename"]}: NOT FOUND')
    
    print(f'\nDone. {len(invoices)} files renamed.')
    print(f'\nFinal files:')
    for f in sorted(os.listdir(args.invoice_dir)):
        print(f'  {f}')

if __name__ == '__main__':
    main()
